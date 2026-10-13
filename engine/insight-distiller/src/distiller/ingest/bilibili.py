"""B 站采集。

核心策略：**优先抓字幕，命中就完全跳过 ASR**——速度快约 50 倍且零成本。
字幕来源优先级：CC 人工字幕 > AI 自动字幕 > 无（落 M2 的 ASR）。

已知平台约束（详见 DISCOVERY.md）：
- 字幕接口需要 WBI 签名，且规则会变动。签名逻辑集中在本文件，
  失效时只改这一处，并由 `distill doctor` 自检。
- 部分字幕需登录态才返回，因此支持从浏览器导入 cookie。
"""

from __future__ import annotations

import functools
import re
import time
import urllib.parse
from datetime import datetime, timezone
from hashlib import md5

from ..models import ContentType, RawContent, Segment
from .base import FetchError, SourceAdapter, register_adapter

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)

_API_VIEW = "https://api.bilibili.com/x/web-interface/view"
_API_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
_API_NAV = "https://api.bilibili.com/x/web-interface/nav"

# WBI 签名用的固定混淆表（B 站前端算法，若接口失效优先检查此处）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


@register_adapter("bilibili", priority=10)
class BilibiliAdapter(SourceAdapter):
    def match(self, url: str) -> bool:
        return "bilibili.com" in url or url.startswith("BV")

    def fetch(self, url: str) -> RawContent:
        bvid = self._parse_id(url)
        info = self._video_info(bvid)

        title = info.get("title") or bvid
        author = (info.get("owner") or {}).get("name")
        duration = info.get("duration")
        pubdate = info.get("pubdate")
        publish_at = (
            datetime.fromtimestamp(pubdate, tz=timezone.utc) if pubdate else None
        )
        cid = info.get("cid") or ((info.get("pages") or [{}])[0].get("cid"))

        warnings: list[str] = []
        segments: list[Segment] = []

        if cid:
            segments, sub_note = self._fetch_subtitle(bvid, cid)
            if sub_note:
                warnings.append(sub_note)
        else:
            warnings.append("未取到 cid，无法获取字幕")

        if not segments:
            desc = (info.get("desc") or "").strip()
            # 简介太短没有信息量（常见为 "-" 或空），不作为兜底内容，
            # 否则会被误判为"已有正文"而跳过转写。
            if len(desc) >= 30:
                segments = [Segment(idx=0, text=desc, section="简介")]
                warnings.append(
                    "该视频无可用字幕，已暂用视频简介占位；将尝试音频转写获取完整内容。"
                )
            else:
                warnings.append("该视频无可用字幕且简介过短，需通过音频转写获取内容。")

        return RawContent(
            url=url,
            content_type=ContentType.VIDEO,
            platform=self.name,
            title=title,
            author=author,
            publish_at=publish_at,
            duration_sec=duration,
            lang="zh",
            segments=segments,
            warnings=warnings,
            extra={"bvid": bvid, "cid": cid},
        )

    # ---------- 内部实现 ----------

    @staticmethod
    def _parse_id(url: str) -> str:
        if m := _BV_RE.search(url):
            return m.group(1)
        if m := _AV_RE.search(url):
            return f"av{m.group(1)}"
        raise FetchError(f"无法从 URL 解析视频 ID: {url}")

    def _headers(self) -> dict[str, str]:
        # B 站接口校验 Referer，缺失会返回 -403
        headers = {"Referer": "https://www.bilibili.com/"}
        if cookie := self._cookie_header():
            headers["Cookie"] = cookie
        return headers

    @functools.lru_cache(maxsize=1)
    def _cookie_header(self) -> str:
        """从浏览器读取 cookie。部分字幕与高清资源需要登录态。

        交给 browser_cookie3 处理各平台的 cookie 存储位置差异，
        不自己拼路径（Windows 与 macOS 位置完全不同）。
        """
        browser = self.config.cookies_from_browser
        if not browser:
            return ""
        try:
            import browser_cookie3

            loader = getattr(browser_cookie3, browser, None)
            if loader is None:
                return ""
            jar = loader(domain_name="bilibili.com")
            return "; ".join(f"{c.name}={c.value}" for c in jar)
        except Exception:  # noqa: BLE001 - 取不到 cookie 只影响部分内容
            return ""

    def _video_info(self, bvid: str) -> dict:
        params = {"bvid": bvid} if bvid.startswith("BV") else {"aid": bvid[2:]}
        resp = self._get(
            _API_VIEW + "?" + urllib.parse.urlencode(params), headers=self._headers()
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FetchError(f"B 站接口返回错误: {data.get('message')}（code={data.get('code')}）")
        return data.get("data") or {}

    def _fetch_subtitle(self, bvid: str, cid: int) -> tuple[list[Segment], str | None]:
        """获取字幕。优先 CC 人工字幕，其次 AI 自动字幕。"""
        try:
            params = self._sign({"bvid": bvid, "cid": cid})
            resp = self._get(
                _API_PLAYER + "?" + urllib.parse.urlencode(params),
                headers=self._headers(),
            )
            data = resp.json()
            if data.get("code") != 0:
                return [], f"字幕接口返回 code={data.get('code')}，可能需要登录"

            subtitles = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not subtitles:
                return [], None

            chosen = self._choose_subtitle(subtitles)
            sub_url = chosen.get("subtitle_url") or ""
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            if not sub_url:
                return [], "字幕地址为空"

            body = self._get(sub_url, headers=self._headers()).json().get("body") or []
            segments = [
                Segment(
                    idx=i,
                    text=(item.get("content") or "").strip(),
                    start_ms=int(float(item.get("from", 0)) * 1000),
                    end_ms=int(float(item.get("to", 0)) * 1000),
                )
                for i, item in enumerate(body)
                if (item.get("content") or "").strip()
            ]
            kind = "AI 自动字幕" if chosen.get("ai_status") else "CC 字幕"
            note = f"已使用{kind}（{chosen.get('lan_doc') or chosen.get('lan')}），跳过音频转写"
            return self._merge_short(segments), note
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001
            return [], f"字幕获取失败: {exc}"

    @staticmethod
    def _choose_subtitle(subtitles: list[dict]) -> dict:
        """优先人工中文字幕，其次任意人工，最后 AI 字幕。"""
        def score(s: dict) -> tuple[int, int]:
            is_ai = 1 if s.get("ai_status") else 0
            lan = (s.get("lan") or "").lower()
            is_zh = 0 if lan.startswith("zh") else 1
            return (is_ai, is_zh)

        return sorted(subtitles, key=score)[0]

    @staticmethod
    def _merge_short(segments: list[Segment], *, target_chars: int = 120) -> list[Segment]:
        """字幕天然是碎句，合并成语义更完整的段落。

        直接把单行字幕送进 LLM 会浪费大量 token 在碎片上，
        合并后既省钱又让摘要质量更好。时间戳保留首句起点。
        """
        merged: list[Segment] = []
        buf: list[Segment] = []

        def flush() -> None:
            if not buf:
                return
            merged.append(
                Segment(
                    idx=len(merged),
                    text="".join(s.text for s in buf),
                    start_ms=buf[0].start_ms,
                    end_ms=buf[-1].end_ms,
                )
            )
            buf.clear()

        for seg in segments:
            buf.append(seg)
            if sum(len(s.text) for s in buf) >= target_chars:
                flush()
        flush()
        return merged

    # ---------- WBI 签名 ----------

    def _sign(self, params: dict) -> dict:
        """WBI 签名。B 站会变更算法，失效时优先检查这里。"""
        try:
            img_key, sub_key = self._wbi_keys()
        except Exception:
            # 拿不到 key 时退化为不签名请求，部分情况下仍可用
            return params

        mixin = "".join(
            (img_key + sub_key)[i] for i in _MIXIN_KEY_ENC_TAB
        )[:32]
        signed = dict(params)
        signed["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(signed.items()))
        signed["w_rid"] = md5((query + mixin).encode("utf-8")).hexdigest()
        return signed

    @functools.lru_cache(maxsize=1)
    def _wbi_keys(self) -> tuple[str, str]:
        resp = self._get(_API_NAV, headers=self._headers())
        wbi = (resp.json().get("data") or {}).get("wbi_img") or {}

        def stem(url: str) -> str:
            return url.rsplit("/", 1)[-1].split(".")[0]

        return stem(wbi.get("img_url", "")), stem(wbi.get("sub_url", ""))

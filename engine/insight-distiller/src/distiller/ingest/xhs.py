"""小红书采集。

**平台现实**（见 DECISIONS.md）：小红书风控最强，需登录态 + 签名，
自动化抓取极易触发验证或封号。因此本采集器的定位是**尽力而为 + 明确降级**：

- 优先从页面内嵌的 __INITIAL_STATE__ 取结构化数据（比解析 DOM 稳）；
- 失败时不抛含糊错误，而是明确告知原因与替代方案（--paste）；
- 不做重试轰炸，避免加剧风控。
"""

from __future__ import annotations

import json
import re

from ..models import ContentType, RawContent, Segment
from .base import FetchError, register_adapter, split_paragraphs
from .web import WebAdapter

_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S
)


@register_adapter("xhs", priority=10)
class XiaohongshuAdapter(WebAdapter):
    def match(self, url: str) -> bool:
        return "xiaohongshu.com" in url or "xhslink.com" in url

    def fetch(self, url: str) -> RawContent:
        headers = {"Referer": "https://www.xiaohongshu.com/"}
        if cookie := self._cookie_header():
            headers["Cookie"] = cookie

        try:
            resp = self._get(url, headers=headers)
        except FetchError as exc:
            raise self._guidance(str(exc)) from exc

        html = resp.text
        if "验证" in html and "滑动" in html:
            raise self._guidance("触发了滑动验证")

        raw = self._from_state(html, url)
        if raw is not None:
            return raw

        # 降级到通用抽取
        try:
            fallback = self.parse_html(html, url, platform=self.name)
        except FetchError as exc:
            raise self._guidance("页面结构无法解析") from exc

        fallback.platform = self.name
        fallback.warnings.append("小红书结构化数据缺失，使用通用抽取，内容可能不完整")
        return fallback

    def _cookie_header(self) -> str:
        browser = self.config.cookies_from_browser
        if not browser:
            return ""
        try:
            import browser_cookie3

            loader = getattr(browser_cookie3, browser, None)
            if loader is None:
                return ""
            return "; ".join(
                f"{c.name}={c.value}"
                for c in loader(domain_name="xiaohongshu.com")
            )
        except Exception:  # noqa: BLE001
            return ""

    def _from_state(self, html: str, url: str) -> RawContent | None:
        """从内嵌 JSON 取笔记内容。比解析 DOM 稳定得多。"""
        m = _STATE_RE.search(html)
        if not m:
            return None
        try:
            # 页面 JSON 可能含 undefined，需替换后才能解析
            state = json.loads(m.group(1).replace("undefined", "null"))
        except json.JSONDecodeError:
            return None

        note = self._find_note(state)
        if not note:
            return None

        title = (note.get("title") or "").strip()
        desc = (note.get("desc") or "").strip()
        if not (title or desc):
            return None

        author = ((note.get("user") or {}).get("nickname")) or None
        paragraphs = split_paragraphs(desc, min_len=2) or [desc]

        return RawContent(
            url=url,
            content_type=ContentType.ARTICLE,
            platform=self.name,
            title=title or paragraphs[0][:50],
            author=author,
            lang="zh",
            segments=[Segment(idx=i, text=p) for i, p in enumerate(paragraphs)],
            warnings=(
                []
                if len(desc) > 100
                else ["小红书笔记正文较短，视频类笔记需等 ASR 转写支持"]
            ),
        )

    @staticmethod
    def _find_note(state: dict) -> dict | None:
        """在嵌套结构中定位笔记数据。页面结构会变，因此做广度搜索而非硬编码路径。"""
        stack: list = [state]
        seen = 0
        while stack and seen < 2000:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                if "desc" in node and ("title" in node or "user" in node):
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return None

    @staticmethod
    def _guidance(reason: str) -> FetchError:
        return FetchError(
            f"小红书采集失败（{reason}）。该平台风控严格，建议：\n"
            "  ① 在 config.yaml 设置 ingest.cookies_from_browser: chrome 复用登录态；\n"
            "  ② 或直接复制笔记正文后执行 distill add \"标题\" --paste。"
        )

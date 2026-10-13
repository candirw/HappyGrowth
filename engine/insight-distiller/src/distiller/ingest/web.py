"""通用网页采集（兜底器，priority 最大）。

trafilatura 正文抽取质量最好；失败时降级到简易 HTML 清洗，
保证不因为单个站点结构特殊就完全拿不到内容。
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import ContentType, RawContent, Segment
from .base import FetchError, SourceAdapter, register_adapter, split_paragraphs


@register_adapter("web", priority=999)
class WebAdapter(SourceAdapter):
    """兜底采集器：匹配任何 http(s) URL。"""

    def match(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def fetch(self, url: str) -> RawContent:
        resp = self._get(url)
        # 显式指定编码优先级：HTTP 头 > HTML meta > UTF-8 猜测
        html = resp.text
        return self.parse_html(html, url)

    def parse_html(self, html: str, url: str, *, platform: str | None = None) -> RawContent:
        title, author, publish_at, text = _extract(html, url)
        if not text.strip():
            raise FetchError(
                "未能抽取到正文。该页面可能需要登录或由 JS 渲染，"
                "可改用 `distill add --paste` 手动粘贴内容。"
            )

        paragraphs = split_paragraphs(text, min_len=2)
        segments = [
            Segment(idx=i, text=p, section=None) for i, p in enumerate(paragraphs)
        ]
        return RawContent(
            url=url,
            content_type=ContentType.ARTICLE,
            platform=platform or self.name,
            title=title or url,
            author=author,
            publish_at=publish_at,
            lang="zh" if _is_mostly_chinese(text) else None,
            segments=segments,
        )


def _extract(html: str, url: str) -> tuple[str, str | None, datetime | None, str]:
    """用 trafilatura 抽正文与元数据，失败则降级。"""
    try:
        import trafilatura
        from trafilatura.settings import use_config

        cfg = use_config()
        # 关闭信号量相关的外部调用，避免在受限环境下卡住
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=False,
            config=cfg,
        ) or ""

        title, author, publish_at = "", None, None
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
            if meta:
                title = meta.title or ""
                author = meta.author
                if meta.date:
                    try:
                        publish_at = datetime.fromisoformat(meta.date)
                    except ValueError:
                        publish_at = None
        except Exception:  # noqa: BLE001 - 元数据抽取失败不应阻断正文
            pass

        if text.strip():
            return title, author, publish_at, text
    except ImportError:
        pass

    return _fallback_extract(html)


_TAG_RE = re.compile(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", re.S | re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_BR_RE = re.compile(r"<(br|/p|/div|/h\d)[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def _fallback_extract(html: str) -> tuple[str, str | None, datetime | None, str]:
    """极简降级抽取：去脚本样式 → 块级标签转换行 → 去标签。"""
    title_match = _TITLE_RE.search(html)
    title = _unescape(title_match.group(1)).strip() if title_match else ""

    body = _TAG_RE.sub(" ", html)
    body = _BR_RE.sub("\n", body)
    body = _ANY_TAG.sub(" ", body)
    text = _unescape(body)
    # 只保留较长的行，过滤导航栏等碎片
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if len(ln) > 15)
    return title, None, None, text


def _unescape(text: str) -> str:
    import html as html_mod

    return html_mod.unescape(text)


def _is_mostly_chinese(text: str) -> bool:
    sample = text[:2000]
    if not sample:
        return False
    han = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return han / len(sample) > 0.2

"""微信公众号采集。

关键难点：**大量公众号文章正文由长图承载**，纯 HTML 抽取会得到空文章。
因此当正文字数异常低时，自动对文中图片跑 OCR。
这不是可选优化，而是这个平台的必需能力。
"""

from __future__ import annotations

import re

from ..models import ContentType, RawContent, Segment
from .base import FetchError, register_adapter, split_paragraphs
from .ocr import get_ocr_engine
from .web import WebAdapter

_IMG_RE = re.compile(r'<img[^>]+data-src="([^"]+)"', re.I)
_TITLE_RE = re.compile(r'<h1[^>]*class="rich_media_title"[^>]*>(.*?)</h1>', re.S | re.I)
_AUTHOR_RE = re.compile(r'var nickname\s*=\s*["\']([^"\']+)["\']')


@register_adapter("wechat", priority=10)
class WeChatAdapter(WebAdapter):
    def match(self, url: str) -> bool:
        return "mp.weixin.qq.com" in url

    def fetch(self, url: str) -> RawContent:
        resp = self._get(url)
        html = resp.text

        if "环境异常" in html or "去验证" in html:
            raise FetchError(
                "公众号返回验证页面。可稍后重试，或用 `distill add --paste` 粘贴正文。"
            )

        raw = self.parse_html_lenient(html, url)

        # 正文过短 → 判定为图片型文章，触发 OCR
        if raw.char_count < self.config.ocr_trigger_chars:
            ocr_text, note = self._ocr_images(html)
            if ocr_text:
                start = len(raw.segments)
                raw.segments.extend(
                    Segment(idx=start + i, text=p, section="OCR")
                    for i, p in enumerate(split_paragraphs(ocr_text, min_len=4))
                )
                raw.warnings.append(note)
            else:
                raw.warnings.append(
                    f"正文仅 {raw.char_count} 字且 OCR 未取到文本"
                    f"（{note}）。可用 --paste 手动补充。"
                )

        if not raw.segments:
            raise FetchError("未能抽取到正文，可用 `distill add --paste` 手动粘贴。")
        return raw

    def parse_html_lenient(self, html: str, url: str) -> RawContent:
        """公众号有稳定的 DOM 结构，优先用专用选择器取标题作者。"""
        try:
            raw = self.parse_html(html, url, platform=self.name)
        except FetchError:
            # 正文可能全是图片，此处不视为失败，交由 OCR 补充
            raw = RawContent(
                url=url,
                content_type=ContentType.ARTICLE,
                platform=self.name,
                title="",
                segments=[],
            )

        if m := _TITLE_RE.search(html):
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if title:
                raw.title = title
        if not raw.author and (m := _AUTHOR_RE.search(html)):
            raw.author = m.group(1)
        if not raw.title:
            raw.title = url
        raw.platform = self.name
        return raw

    def _ocr_images(self, html: str, *, max_images: int = 12) -> tuple[str, str]:
        engine = get_ocr_engine(self.config.ocr_engine)
        if engine is None:
            return "", "未安装 OCR 引擎，可执行 pip install -e '.[ocr]'"

        urls = _IMG_RE.findall(html)[:max_images]
        if not urls:
            return "", "文中未找到图片"

        texts: list[str] = []
        failed = 0
        for img_url in urls:
            try:
                resp = self._get(img_url)
                text = engine.read(resp.content)
                if text.strip():
                    texts.append(text.strip())
            except Exception:  # noqa: BLE001 - 单张图失败不影响其余
                failed += 1

        note = (
            f"检测为图片型文章，已用 {getattr(engine, 'name', 'ocr')} "
            f"识别 {len(texts)}/{len(urls)} 张图片"
        )
        if failed:
            note += f"（{failed} 张失败）"
        return "\n\n".join(texts), note

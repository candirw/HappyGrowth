"""手动粘贴与本地文本兜底。

**设计原则**：任何平台的反爬、登录墙、JS 渲染都不应让工具完全不可用。
采集与浓缩彻底解耦，只要能拿到文本就能跑完整条链路。
"""

from __future__ import annotations

from pathlib import Path

from ..models import ContentType, RawContent, Segment
from .base import FetchError, SourceAdapter, register_adapter, split_paragraphs


class PasteAdapter(SourceAdapter):
    """不参与 URL 匹配，由 CLI 的 --paste / --file 显式调用。"""

    name = "paste"

    def match(self, url: str) -> bool:
        return False

    def fetch(self, url: str) -> RawContent:  # pragma: no cover
        raise NotImplementedError("请使用 from_text / from_file")

    def from_text(
        self, text: str, *, title: str | None = None, url: str | None = None
    ) -> RawContent:
        paragraphs = split_paragraphs(text, min_len=2)
        if not paragraphs:
            raise ValueError("粘贴的内容为空")

        if not title:
            # 用首行作标题，截断避免过长
            title = paragraphs[0][:60]

        return RawContent(
            url=url or f"paste://{abs(hash(text[:200]))}",
            content_type=ContentType.ARTICLE,
            platform="paste",
            title=title,
            segments=[Segment(idx=i, text=p) for i, p in enumerate(paragraphs)],
            warnings=["内容由手动粘贴导入"],
        )

    def from_file(self, path: str | Path) -> RawContent:
        from ..compat.fsio import read_text

        p = Path(path).expanduser()
        if not p.exists():
            raise FetchError(f"文件不存在：{p}")
        if p.is_dir():
            raise FetchError(f"这是一个目录，不是文件：{p}")

        try:
            text = read_text(p)  # 强制 UTF-8，避免 Windows 默认 GBK 乱码
        except UnicodeDecodeError as exc:
            # 二进制文件（含未被识别的音视频）走到这里，
            # 报「编码错误」会让人以为是编码问题，实则是格式不对
            raise FetchError(
                f"无法以文本方式读取「{p.name}」。\n"
                f"  若是音视频文件，本工具支持直接转写，但该后缀未被识别；\n"
                f"  若是文本文件，请确认编码为 UTF-8。"
            ) from exc
        return self.from_text(text, title=p.stem, url=f"file://{p.name}")

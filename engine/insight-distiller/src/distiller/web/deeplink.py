"""深链构建：把锚点转成可跳转回原位置的 URL。

**这是 Web UI 相对 CLI 的核心增量**——CLI 里看到 `12:34` 只能自己去
拖进度条，浏览器里应该点一下就跳到视频那一秒。

锚点有三种形态（见 `models.Segment.anchor`）：
- `HH:MM:SS` / `MM:SS` —— 音视频时间戳，可转成 URL 时间参数
- 章节名 —— 文章/论文，无法精确跳转，退化为原文链接
- `#12` —— 片段序号，同上

各平台时间参数格式不同且**不能猜**：写错会跳到视频开头而不报错，
属于静默失败，所以只对已确认格式的平台做转换，其余原样返回。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 纯数字锚点（#12）与章节名都无法定位到时间
_TS_RE = re.compile(r"^(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)$")


def anchor_to_seconds(anchor: str | None) -> int | None:
    """把 `HH:MM:SS` / `MM:SS` 锚点转成秒。非时间锚点返回 None。"""
    if not anchor:
        return None
    m = _TS_RE.match(anchor.strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    return hours * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _set_query(url: str, params: dict[str, str]) -> str:
    """合并 query 参数。保留原有参数（B 站的 p= 分 P 参数不能丢）。"""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))


def _bilibili(url: str, seconds: int) -> str:
    # B 站接受 ?t=秒，对分 P 视频需同时保留 p 参数
    return _set_query(url, {"t": str(seconds)})


def _youtube(url: str, seconds: int) -> str:
    host = urlparse(url).netloc.lower()
    if "youtu.be" in host:
        return _set_query(url, {"t": str(seconds)})
    # watch?v= 形式用 t=123s；embed 用 start=
    if "/embed/" in url:
        return _set_query(url, {"start": str(seconds)})
    return _set_query(url, {"t": f"{seconds}s"})


def _generic_fragment(url: str, seconds: int) -> str:
    """媒体片段规范（RFC 5147 风格），本地音视频与部分播放器支持。"""
    base = url.split("#", 1)[0]
    return f"{base}#t={seconds}"


# 只登记已确认格式的平台。猜错会静默跳到开头，比不跳更糟。
_BUILDERS = {
    "bilibili": _bilibili,
    "youtube": _youtube,
    "local": _generic_fragment,
    "paste": None,
    "wechat": None,   # 公众号文章无时间维度
    "zhihu": None,
    "xhs": None,      # 小红书视频页不支持 URL 定位
    "web": None,
    "arxiv": None,
}


def supports_timestamp(platform: str | None) -> bool:
    """该平台能否按时间跳转。用于决定锚点渲染成链接还是纯文本。"""
    if not platform:
        return False
    return _BUILDERS.get(platform.lower()) is not None


def deeplink(
    url: str,
    *,
    platform: str | None = None,
    anchor: str | None = None,
    seconds: int | None = None,
) -> str:
    """构建跳转链接。无法定位时返回原始 url（保证永远可点）。

    seconds 优先于 anchor —— 有 `Segment.start_ms` 时应直接传秒，
    避免「格式化成字符串再解析回来」的精度损失。
    """
    if not url:
        return ""

    offset = seconds if seconds is not None else anchor_to_seconds(anchor)
    if offset is None or offset < 0:
        return url

    builder = _BUILDERS.get((platform or "").lower())
    if builder is None:
        # 平台未登记：不猜参数名，原样返回
        return url
    try:
        return builder(url, offset)
    except Exception:  # noqa: BLE001 - 链接构建失败不该让页面 500
        return url


def format_seconds(total: int | None) -> str:
    """秒转 `HH:MM:SS`（不足 1 小时用 `MM:SS`），用于展示。"""
    if total is None or total < 0:
        return ""
    h, rem = divmod(int(total), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


__all__ = [
    "anchor_to_seconds",
    "deeplink",
    "format_seconds",
    "supports_timestamp",
]

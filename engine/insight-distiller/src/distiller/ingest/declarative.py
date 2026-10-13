"""声明式站点采集：用 CSS 选择器在 config.yaml 里加一个站点，不写代码。

**适用边界必须说清楚**，否则会给人错误期待：

| 能配置 | 不能配置 |
|--------|---------|
| 正文在固定 DOM 节点里的站点（个人博客、新闻、文档站） | 需要请求签名的（B 站字幕的 WBI、抖音的设备指纹） |
| 需要去掉导航/评论等噪声节点的站点 | 内容在嵌套 JSON 里且路径会变的（小红书要做广度搜索定位） |
| 标题/作者/日期位置稳定的站点 | 需要私有 API 拼接的（知乎的 /api/v4/...） |
| | 纯前端渲染、HTML 里没有正文的 |

换句话说：**声明式解决「解析」，解决不了「拿到」。**
拿不到内容的平台，配一百个选择器也没用。

这类站点在「通用网页」里占大多数，而 trafilatura 的通用抽取
对结构特殊的页面（带侧栏的文档站、评论区很长的博客）经常抽进噪声，
所以这个能力的实际价值不低。
"""

from __future__ import annotations

from ..config import IngestConfig, SiteRule
from ..models import ContentType, RawContent, Segment
from .base import FetchError, SourceAdapter, split_paragraphs

# 介于内置专用采集器（10）与通用兜底（100）之间：
# 用户显式写的规则应当优先于通用抽取，但不该盖掉专用采集器
_SITE_PRIORITY = 50


class DeclarativeAdapter(SourceAdapter):
    """由 SiteRule 驱动的采集器。

    不走 @register_adapter：规则来自配置，每次调用按当前配置动态构造，
    这样改完 config.yaml 立即生效，不需要重启或重新注册。
    """

    priority = _SITE_PRIORITY

    def __init__(self, config: IngestConfig, rule: SiteRule) -> None:
        self.rule = rule
        # name 要在 super().__init__ 之前设好——父类用它取平台级配置
        self.name = rule.name
        super().__init__(config)

    def match(self, url: str) -> bool:
        return any(m and m in url for m in self.rule.match)

    def fetch(self, url: str) -> RawContent:
        try:
            import lxml.html
        except ImportError as exc:  # pragma: no cover
            raise FetchError(
                "声明式站点规则需要 lxml：pip install -e '.[search]'"
            ) from exc

        resp = self._get(url)
        doc = lxml.html.fromstring(resp.text)

        # 先删噪声再抽正文，否则 text_content() 会把评论、导航一起带进来
        for sel in self.rule.remove:
            for node in self._select(doc, sel):
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)

        title = self._first_text(doc, self.rule.title) or ""
        if not title:
            # 标题缺失不致命，退回 <title>
            head = doc.findtext(".//title") or ""
            title = head.strip() or url

        body = self._collect_text(doc, self.rule.content)
        if not body.strip():
            raise FetchError(
                f"站点规则「{self.rule.name}」未抽到正文。\n"
                f"  请检查 content 选择器是否仍然有效：{self.rule.content!r}\n"
                f"  站点改版会导致选择器失效；也可临时删掉该规则改用通用抽取。"
            )

        paragraphs = split_paragraphs(body, min_len=2) or [body.strip()]
        return RawContent(
            url=url,
            content_type=ContentType.ARTICLE,
            platform=self.rule.name,
            title=title.strip()[:200],
            author=self._first_text(doc, self.rule.author),
            lang=None,
            segments=[Segment(idx=i, text=p) for i, p in enumerate(paragraphs)],
            warnings=[f"由站点规则「{self.rule.name}」抽取"],
        )

    # ---------- 选择器 ----------

    @staticmethod
    def _select(doc, selector: str | None) -> list:
        """CSS 选择器；以 / 开头则按 XPath 处理。

        同时支持两种是有原因的：CSS 覆盖 95% 场景且大家都熟，
        但少数情况（按文本定位、取属性）只有 XPath 能表达。
        """
        if not selector:
            return []
        try:
            if selector.startswith("/"):
                return doc.xpath(selector)
            return doc.cssselect(selector)
        except ImportError as exc:  # cssselect 未安装
            raise FetchError(
                "CSS 选择器需要 cssselect：pip install cssselect\n"
                "  或把选择器改写为 XPath（以 / 开头）。"
            ) from exc
        except Exception:  # noqa: BLE001 - 选择器语法错误不该让整个流程崩
            return []

    def _first_text(self, doc, selector: str | None) -> str | None:
        nodes = self._select(doc, selector)
        if not nodes:
            return None
        node = nodes[0]
        text = node if isinstance(node, str) else node.text_content()
        return (text or "").strip() or None

    def _collect_text(self, doc, selector: str | None) -> str:
        nodes = self._select(doc, selector)
        if not nodes:
            return ""
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, str):
                parts.append(node)
                continue
            # 逐个块级子节点取文本，保住段落边界；
            # 直接 text_content() 会把所有段落挤成一行
            blocks = node.xpath(".//p | .//li | .//h2 | .//h3 | .//blockquote | .//pre")
            if blocks:
                parts.extend((b.text_content() or "").strip() for b in blocks)
            else:
                parts.append((node.text_content() or "").strip())
        return "\n\n".join(p for p in parts if p)


def build_site_adapters(config: IngestConfig) -> list[DeclarativeAdapter]:
    """按配置构造声明式采集器。重名或缺 match 的规则跳过。"""
    out: list[DeclarativeAdapter] = []
    seen: set[str] = set()
    for rule in config.sites:
        if not rule.enabled or not rule.name or not rule.match:
            continue
        if rule.name in seen:
            continue
        seen.add(rule.name)
        out.append(DeclarativeAdapter(config, rule))
    return out


__all__ = ["DeclarativeAdapter", "build_site_adapters"]

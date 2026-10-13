"""采集层测试：平台开关、参数覆盖、声明式站点规则。

重点是「禁用后不静默降级」这条语义——通用兜底器对所有 URL
都 match，不特殊处理会让禁用变成悄悄给用户残缺内容。
"""

from __future__ import annotations

import pytest

from distiller.config import IngestConfig, PlatformConfig, SiteRule
from distiller.ingest import FetchError, pick_adapter



class TestPlatformConfig:
    """平台开关与参数覆盖。

    核心语义：禁用专用采集器时必须**明确报错**，不能静默降级到
    通用抽取——小红书正文在嵌套 JSON、B 站正文在字幕接口，
    通用抽取只会拿到空内容或噪声，用户却不知道原因。
    """

    def test_default_all_enabled(self) -> None:
        cfg = IngestConfig()
        for name in ("bilibili", "wechat", "xhs", "zhihu", "web"):
            assert cfg.is_enabled(name)

    def test_disable_blocks_instead_of_degrading(self) -> None:
        """回归测试：web 采集器对所有 URL 都 match，
        不特殊处理会让禁用变成静默降级。"""
        cfg = IngestConfig(platforms={"xhs": PlatformConfig(enabled=False)})
        with pytest.raises(FetchError, match="已在配置中禁用"):
            pick_adapter("https://www.xiaohongshu.com/explore/abc", cfg)

    def test_disable_one_does_not_affect_others(self) -> None:
        cfg = IngestConfig(platforms={"xhs": PlatformConfig(enabled=False)})
        assert pick_adapter("https://example.com/p/1", cfg).name == "web"
        assert pick_adapter(
            "https://www.bilibili.com/video/BV1xx", cfg
        ).name == "bilibili"

    def test_platform_overrides_global(self) -> None:
        cfg = IngestConfig(
            cookies_from_browser="chrome",
            platforms={"bilibili": PlatformConfig(cookies_from_browser="edge")},
        )
        assert cfg.effective("bilibili").cookies_from_browser == "edge"
        # 未覆盖的沿用全局
        assert cfg.effective("zhihu").cookies_from_browser == "chrome"

    def test_adapter_sees_merged_config(self) -> None:
        cfg = IngestConfig(
            timeout=30,
            platforms={"bilibili": PlatformConfig(timeout=90)},
        )
        assert pick_adapter("https://www.bilibili.com/video/BV1xx", cfg).config.timeout == 90
        assert pick_adapter("https://zhihu.com/question/1/answer/2", cfg).config.timeout == 30

    def test_rate_limit_configured_per_platform(self) -> None:
        cfg = IngestConfig(platforms={"xhs": PlatformConfig(rate_limit_sec=3.0)})
        assert cfg.platform("xhs").rate_limit_sec == 3.0
        assert cfg.platform("zhihu").rate_limit_sec == 0.0


class TestDeclarativeSites:
    """声明式站点规则。

    只覆盖「正文在固定 DOM 节点」的站点——需要签名或私有 API
    的平台无法用选择器表达，必须写采集器。
    """

    HTML = """<html><head><title>页面标题</title></head><body>
    <nav>首页 关于</nav>
    <h1 class="t">深入理解混合检索</h1>
    <span class="a">张三</span>
    <div class="c">
      <p>向量检索擅长语义匹配。</p>
      <p>BM25 正好互补。</p>
    </div>
    <div class="cm"><p>垃圾评论</p></div>
    </body></html>"""

    def _adapter(self, rule: SiteRule, cfg: IngestConfig | None = None):
        from distiller.ingest.declarative import DeclarativeAdapter

        ad = DeclarativeAdapter(cfg or IngestConfig(), rule)
        ad._get = lambda url, **kw: type("R", (), {"text": self.HTML})()
        return ad

    def _rule(self, **kw) -> SiteRule:
        base = dict(
            name="blog", match=["t.local"],
            title="h1.t", content="div.c", author="span.a",
            remove=["nav", "div.cm"],
        )
        base.update(kw)
        return SiteRule(**base)

    def test_extracts_fields(self) -> None:
        raw = self._adapter(self._rule()).fetch("https://t.local/p/1")
        assert raw.title == "深入理解混合检索"
        assert raw.author == "张三"
        assert raw.platform == "blog"

    def test_preserves_paragraph_boundaries(self) -> None:
        """直接 text_content() 会把段落挤成一行，必须逐块取。"""
        raw = self._adapter(self._rule()).fetch("https://t.local/p/1")
        assert len(raw.segments) == 2

    def test_removes_noise_nodes(self) -> None:
        raw = self._adapter(self._rule()).fetch("https://t.local/p/1")
        body = " ".join(s.text for s in raw.segments)
        assert "垃圾评论" not in body
        assert "首页" not in body

    def test_falls_back_to_title_tag(self) -> None:
        raw = self._adapter(self._rule(title="h9.nonexistent")).fetch("https://t.local/p/1")
        assert raw.title == "页面标题"

    def test_empty_content_gives_actionable_error(self) -> None:
        """站点改版导致选择器失效时，要指出是哪个选择器。"""
        with pytest.raises(FetchError, match="未抽到正文"):
            self._adapter(self._rule(content="div.nonexistent")).fetch("https://t.local/p/1")

    def test_site_rule_beats_generic_fallback(self) -> None:
        cfg = IngestConfig(sites=[self._rule(match=["example.com"])])
        ad = pick_adapter("https://example.com/p/1", cfg)
        assert ad.name == "blog"
        assert ad.priority < 100

    def test_site_rule_does_not_override_builtin(self) -> None:
        """用户为 bilibili 写选择器没有意义——字幕逻辑无法用选择器表达。"""
        cfg = IngestConfig(
            sites=[SiteRule(name="fake", match=["bilibili.com"], content="div")]
        )
        assert pick_adapter("https://www.bilibili.com/video/BV1xx", cfg).name == "bilibili"

    def test_disabled_rule_ignored(self) -> None:
        cfg = IngestConfig(sites=[self._rule(match=["example.com"], enabled=False)])
        assert pick_adapter("https://example.com/p/1", cfg).name == "web"

    def test_rule_without_match_ignored(self) -> None:
        from distiller.ingest.declarative import build_site_adapters

        cfg = IngestConfig(sites=[SiteRule(name="x", match=[], content="div")])
        assert not build_site_adapters(cfg)

    def test_duplicate_names_deduped(self) -> None:
        from distiller.ingest.declarative import build_site_adapters

        cfg = IngestConfig(
            sites=[
                SiteRule(name="dup", match=["a.com"], content="div"),
                SiteRule(name="dup", match=["b.com"], content="div"),
            ]
        )
        assert len(build_site_adapters(cfg)) == 1

    def test_xpath_selector_supported(self) -> None:
        """少数情况（按文本定位、取属性）只有 XPath 能表达。"""
        raw = self._adapter(self._rule(title="//h1[@class='t']")).fetch("https://t.local/p/1")
        assert raw.title == "深入理解混合检索"

    def test_bad_selector_does_not_crash(self) -> None:
        """选择器语法错误不该让整个流程崩，应退化为空结果。"""
        with pytest.raises(FetchError, match="未抽到正文"):
            self._adapter(self._rule(content="!!!invalid((")).fetch("https://t.local/p/1")

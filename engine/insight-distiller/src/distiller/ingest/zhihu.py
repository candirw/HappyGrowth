"""知乎采集（专栏文章 / 回答）。

平台特点（见 DISCOVERY.md）：知乎列表接口直接返回 voteup_count / favorite_count，
是最容易做精确排序的平台，因此"收藏量前 N"这类需求优先在知乎落地（M2.5）。

本文件只做单篇采集。已知约束：需登录态才能看完整内容，
因此失败时明确提示用 --paste 兜底，不让整个流程卡住。
"""

from __future__ import annotations

import re

from ..models import ContentType, RawContent, Segment
from .base import FetchError, register_adapter, split_paragraphs
from .web import WebAdapter

_ARTICLE_RE = re.compile(r"zhuanlan\.zhihu\.com/p/(\d+)")
_ANSWER_RE = re.compile(r"zhihu\.com/question/(\d+)/answer/(\d+)")

_API_ARTICLE = "https://www.zhihu.com/api/v4/articles/{}"
_API_ANSWER = "https://www.zhihu.com/api/v4/answers/{}"


@register_adapter("zhihu", priority=10)
class ZhihuAdapter(WebAdapter):
    def match(self, url: str) -> bool:
        return "zhihu.com" in url

    def fetch(self, url: str) -> RawContent:
        if m := _ARTICLE_RE.search(url):
            return self._fetch_api(_API_ARTICLE.format(m.group(1)), url, "article")
        if m := _ANSWER_RE.search(url):
            return self._fetch_api(_API_ANSWER.format(m.group(2)), url, "answer")
        # 问题页等其他形态走通用抽取
        return self._fetch_html(url)

    def _headers(self) -> dict[str, str]:
        headers = {"Referer": "https://www.zhihu.com/"}
        if cookie := self._cookie_header():
            headers["Cookie"] = cookie
        return headers

    def _cookie_header(self) -> str:
        """知乎有登录墙，复用浏览器 cookie。

        各平台 cookie 存储位置差异交给 browser_cookie3，不自己拼路径。
        """
        browser = self.config.cookies_from_browser
        if not browser:
            return ""
        try:
            import browser_cookie3

            loader = getattr(browser_cookie3, browser, None)
            if loader is None:
                return ""
            return "; ".join(
                f"{c.name}={c.value}" for c in loader(domain_name="zhihu.com")
            )
        except Exception:  # noqa: BLE001 - 取不到 cookie 只影响部分内容
            return ""

    def _fetch_api(self, api_url: str, origin_url: str, kind: str) -> RawContent:
        try:
            resp = self._get(api_url, headers=self._headers())
            data = resp.json()
        except Exception:  # noqa: BLE001 - API 失败降级到 HTML
            return self._fetch_html(origin_url)

        html_body = data.get("content") or ""
        if not html_body:
            return self._fetch_html(origin_url)

        text = re.sub(r"<[^>]+>", "\n", html_body)
        import html as html_mod

        text = html_mod.unescape(text)
        paragraphs = split_paragraphs(text, min_len=2)
        if not paragraphs:
            return self._fetch_html(origin_url)

        author = ((data.get("author") or {}).get("name")) or None
        title = data.get("title") or (
            (data.get("question") or {}).get("title")
        ) or origin_url

        extra = {
            "voteup_count": data.get("voteup_count"),
            "favorite_count": data.get("favorite_count") or data.get("collect_count"),
            "kind": kind,
        }

        return RawContent(
            url=origin_url,
            content_type=ContentType.ARTICLE,
            platform=self.name,
            title=title,
            author=author,
            lang="zh",
            segments=[Segment(idx=i, text=p) for i, p in enumerate(paragraphs)],
            extra={k: v for k, v in extra.items() if v is not None},
        )

    def _fetch_html(self, url: str) -> RawContent:
        try:
            resp = self._get(url, headers=self._headers())
            raw = self.parse_html(resp.text, url, platform=self.name)
        except FetchError as exc:
            raise FetchError(
                f"知乎内容抽取失败（{exc}）。知乎有登录墙，可在 config.yaml 设置 "
                "ingest.cookies_from_browser: chrome，或用 `distill add x --paste` 粘贴正文。"
            ) from exc
        raw.platform = self.name
        return raw

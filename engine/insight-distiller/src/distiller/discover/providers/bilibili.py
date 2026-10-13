"""B 站发现器。

复用采集层的 BilibiliAdapter 处理 WBI 签名与 cookie，避免两处维护。

已实测确认的平台约束（详见 DISCOVERY.md）：
- 空间搜索**只支持按播放量/日期/评论数排序，没有收藏量排序**；
- 投稿列表接口**不返回收藏数**，需逐条查详情补齐 → 有请求量代价；
- 因此「收藏量前 100」的实现是：拉播放量 Top N*3 候选池 → 补齐收藏数 → 本地重排，
  并明确标注这是**近似值**，不伪装精确。
"""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime, timezone

from ...config import IngestConfig
from ...ingest.bilibili import BilibiliAdapter
from ..models import Candidate, Platform, Query, RankBy, TargetType
from .base import DiscoveryError, DiscoveryProvider, register_provider

_API_SEARCH_USER = "https://api.bilibili.com/x/web-interface/wbi/search/type"
_API_SPACE_SEARCH = "https://api.bilibili.com/x/space/wbi/arc/search"
_API_VIEW = "https://api.bilibili.com/x/web-interface/view"

# 空间搜索（UP 主投稿）支持的排序键
_SPACE_ORDER = {
    RankBy.PLAY: "click",
    RankBy.DATE: "pubdate",
    RankBy.COMMENTS: "dm",
}

# 全站关键词搜索支持的排序键。
# RELEVANCE 对应 totalrank（B 站搜索框的默认「综合排序」），
# 这是原生支持的，不需要扩大候选池本地重排。
_SEARCH_ORDER = {
    RankBy.RELEVANCE: "totalrank",
    RankBy.PLAY: "click",
    RankBy.DATE: "pubdate",
    RankBy.COMMENTS: "dm",
}

_PAGE_SIZE = 30

# 单次检索的候选池上限。
# 放到 1000 是为了支持「抓某 UP 主某系列全部」这类需求——
# 长寿系列动辄 200+ 期，而 UP 主投稿总数可能好几百。
# 仍保留上限是防止误输入（如 limit=99999）打出几十页请求触发风控。
_MAX_POOL = 1000
_MAX_PAGE = 40


@register_provider(Platform.BILIBILI)
class BilibiliDiscovery(DiscoveryProvider):
    def __init__(self, config: IngestConfig) -> None:
        super().__init__(config)
        # 复用采集器的签名与 cookie 逻辑，不重复实现
        self._adapter = BilibiliAdapter(config)

    def supports_metric(self, metric: str) -> bool:
        try:
            return RankBy(metric) in _SPACE_ORDER
        except ValueError:
            return False

    # ---------- 实体消歧 ----------

    def resolve(self, name: str) -> list[tuple[str, str, str]]:
        """搜 UP 主，返回候选供用户选择。

        同名账号很常见（"李哥"可能命中十几个），必须让用户确认，
        否则会抓错人的内容——这是典型的"猜错代价高"场景。
        """
        params = self._adapter._sign(
            {"search_type": "bili_user", "keyword": name, "page": 1}
        )
        try:
            resp = self._adapter._get(
                _API_SEARCH_USER + "?" + urllib.parse.urlencode(params),
                headers=self._adapter._headers(),
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DiscoveryError(f"搜索 UP 主失败: {exc}") from exc

        if data.get("code") != 0:
            raise DiscoveryError(
                f"B 站搜索接口返回 code={data.get('code')}（{data.get('message')}）。"
                f"该接口通常需要登录态，可在 config.yaml 设置 "
                f"ingest.cookies_from_browser: chrome"
            )

        results = (data.get("data") or {}).get("result") or []
        candidates: list[tuple[str, str, str]] = []
        for item in results[:8]:
            mid = str(item.get("mid") or "")
            uname = item.get("uname") or ""
            if not mid or not uname:
                continue
            fans = item.get("fans") or 0
            videos = item.get("videos") or 0
            sign = (item.get("usign") or "").strip().replace("\n", " ")[:40]
            desc = f"{_fmt_count(fans)}粉丝 · {videos}投稿"
            if sign:
                desc += f" · {sign}"
            candidates.append((mid, uname, desc))
        return candidates

    # ---------- 发现 ----------

    def discover(self, query: Query) -> list[Candidate]:
        target = query.targets[0]
        if target.type is TargetType.UPLOADER:
            return self._discover_uploader(query)
        if target.type is TargetType.KEYWORD:
            return self._discover_keyword(query)
        raise DiscoveryError(
            f"B 站发现暂不支持 {target.type.value} 类型。"
            f"当前支持：UP 主投稿、关键词搜索。"
        )

    def _discover_uploader(self, query: Query) -> list[Candidate]:
        target = query.targets[0]
        mid = target.resolved_id
        if not mid:
            raise DiscoveryError("UP 主未消歧，无法拉取投稿列表")

        order = _SPACE_ORDER.get(query.rank.by, "click")
        # 不支持的排序（如收藏量）需拉更大候选池后本地重排
        pool_size = query.limit if query.rank.by in _SPACE_ORDER else query.limit * 3

        # 系列名过滤时要**大幅扩池**。
        # 实测：B 站空间搜索的 keyword 是模糊匹配，几乎不起过滤作用
        # （「每天一个宝藏问题」把 399 条投稿只缩到 386 条），
        # 真正生效的是本地标题过滤。若不扩池，用户要 12 条系列视频、
        # 而投稿里系列占比约一半时，只拉 12 条会被滤到剩 6 条。
        if query.filters.include_keywords:
            pool_size = max(pool_size, query.limit * 4)
        pool_size = min(max(pool_size, query.limit), _MAX_POOL)

        # keyword 仍然传：虽是模糊匹配，但能让平台优先返回相关结果
        space_keyword = " ".join(query.filters.include_keywords).strip()

        candidates: list[Candidate] = []
        page = 1
        # 翻页上限由池子大小推出，而不是写死。
        # 曾写死 10 页（=300 条），抓「全系列」时会在第 300 条静默截断——
        # 用户要 211 期却只拿到一部分，且没有任何提示。
        max_page = min(_MAX_PAGE, -(-pool_size // _PAGE_SIZE) + 2)
        while len(candidates) < pool_size and page <= max_page:
            self.limiter.wait()
            params = self._adapter._sign(
                {
                    "mid": mid,
                    "ps": _PAGE_SIZE,
                    "pn": page,
                    "order": order,
                    "keyword": space_keyword,
                }
            )
            try:
                resp = self._adapter._get(
                    _API_SPACE_SEARCH + "?" + urllib.parse.urlencode(params),
                    headers=self._adapter._headers(),
                )
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                if candidates:
                    break  # 已有部分结果，不因单页失败全盘失败
                raise DiscoveryError(f"拉取投稿列表失败: {exc}") from exc

            if data.get("code") != 0:
                if candidates:
                    break
                raise DiscoveryError(
                    f"B 站接口返回 code={data.get('code')}（{data.get('message')}）。"
                    f"投稿列表接口需要登录态，请配置 ingest.cookies_from_browser"
                )

            vlist = (
                ((data.get("data") or {}).get("list") or {}).get("vlist") or []
            )
            if not vlist:
                break

            for item in vlist:
                cand = self._to_candidate(item, author=target.resolved_name)
                if cand:
                    candidates.append(cand)
            page += 1

        return candidates

    def _discover_keyword(self, query: Query) -> list[Candidate]:
        target = query.targets[0]
        keyword = target.raw
        if not keyword:
            raise DiscoveryError("缺少搜索关键词")

        order = _SEARCH_ORDER.get(query.rank.by, "totalrank")

        # 平台原生支持该排序就按需取；不支持（如收藏量）才扩大候选池本地重排。
        # 这里此前误用 _SPACE_ORDER 判断，导致 relevance 也被当成「不支持」
        # 而抓了 3 倍候选——limit=20 时白跑 2 页请求。
        pool_size = min(
            query.limit if query.rank.by in _SEARCH_ORDER else query.limit * 3, 200
        )
        candidates: list[Candidate] = []
        page = 1

        while len(candidates) < pool_size and page <= 6:
            self.limiter.wait()
            params = self._adapter._sign(
                {
                    "search_type": "video",
                    "keyword": keyword,
                    "order": order,
                    "page": page,
                }
            )
            try:
                resp = self._adapter._get(
                    _API_SEARCH_USER + "?" + urllib.parse.urlencode(params),
                    headers=self._adapter._headers(),
                )
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                if candidates:
                    break
                raise DiscoveryError(f"关键词搜索失败: {exc}") from exc

            if data.get("code") != 0:
                if candidates:
                    break
                raise DiscoveryError(
                    f"B 站搜索返回 code={data.get('code')}，通常需要登录态"
                )

            results = (data.get("data") or {}).get("result") or []
            if not results:
                if page == 1 and not candidates:
                    # B 站搜索被限流时返回的是 code=0 + 空 result，
                    # 和「确实没有这个内容」在响应上无法区分。
                    # 静默返回 0 条会让用户以为内容不存在，
                    # 而实际上等几分钟或配 cookie 就能拿到结果。
                    raise DiscoveryError(
                        f"B 站搜索「{keyword}」返回 0 条。\n"
                        "  搜索接口对未登录/频繁请求会返回空结果，与「确实没有」无法区分。\n"
                        "  建议依次尝试：\n"
                        "    ① 等 1-2 分钟再试（最常见原因是刚才请求过频）；\n"
                        "    ② 在 config.yaml 设置 ingest.cookies_from_browser: chrome；\n"
                        "    ③ 给该平台加限流 ingest.platforms.bilibili.rate_limit_sec: 2.0；\n"
                        "    ④ 确认关键词在 B 站站内能搜到结果。"
                    )
                break
            for item in results:
                cand = self._to_candidate(item)
                if cand:
                    candidates.append(cand)
            page += 1

        return candidates

    # ---------- 元数据补齐 ----------

    def enrich_favorites(self, candidates: list[Candidate], *, limit: int) -> int:
        """逐条补齐收藏数（投稿列表不返回该字段）。

        有请求量代价，因此只补前 limit 条，且带限流。
        返回成功补齐的数量。
        """
        filled = 0
        for cand in candidates[:limit]:
            if cand.favorite_count is not None:
                continue
            bvid = _extract_bvid(cand.url)
            if not bvid:
                continue
            self.limiter.wait()
            try:
                resp = self._adapter._get(
                    f"{_API_VIEW}?bvid={bvid}", headers=self._adapter._headers()
                )
                data = resp.json()
                if data.get("code") != 0:
                    continue
                stat = (data.get("data") or {}).get("stat") or {}
                cand.favorite_count = stat.get("favorite")
                cand.like_count = stat.get("like")
                if cand.play_count is None:
                    cand.play_count = stat.get("view")
                filled += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响整体
                continue
        return filled

    @staticmethod
    def _to_candidate(item: dict, *, author: str | None = None) -> Candidate | None:
        bvid = item.get("bvid") or ""
        if not bvid:
            return None

        title = _strip_tags(str(item.get("title") or ""))
        pubdate = item.get("created") or item.get("pubdate") or item.get("senddate")
        published: date | None = None
        if pubdate:
            try:
                published = datetime.fromtimestamp(
                    int(pubdate), tz=timezone.utc
                ).date()
            except (ValueError, OSError):
                published = None

        return Candidate(
            url=f"https://www.bilibili.com/video/{bvid}",
            title=title or bvid,
            platform="bilibili",
            author=item.get("author") or author,
            published=published,
            duration_sec=_parse_duration(item.get("length") or item.get("duration")),
            play_count=_as_int(item.get("play") or item.get("view")),
            comment_count=_as_int(item.get("comment") or item.get("video_review")),
            favorite_count=_as_int(item.get("favorites")),
        )


# ---------- 工具函数 ----------


def _strip_tags(text: str) -> str:
    """清理 B 站搜索结果标题。

    两步都必需：搜索结果含 <em> 高亮标签，且标题经过 HTML 实体转义
    （`&amp;` `&quot;` 等）。不解码会让标题带着转义符进入摘要与文件名。
    """
    import html as html_mod
    import re

    cleaned = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(cleaned).strip()


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _parse_duration(value: object) -> int | None:
    """时长可能是 "12:34" 字符串或秒数整数。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        seconds = 0
        for n in nums:
            seconds = seconds * 60 + n
        return seconds
    return _as_int(value)


def _extract_bvid(url: str) -> str | None:
    import re

    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    return m.group(1) if m else None


def _fmt_count(n: int) -> str:
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)

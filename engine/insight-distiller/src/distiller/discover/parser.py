"""自然语言 → Query IR 解析。

两层设计：
1. **规则层先跑**：日期、数量、时长、平台这些能确定性提取的，绝不交给 LLM
   （LLM 算"最近半年"的具体日期经常错）。
2. **LLM 层补语义**：博主名、主题方向、意图判断，这些规则做不了。

LLM 输出一律视为不可信，解析后用规则校正与兜底。
即使完全没有可用 LLM，规则层也能处理大部分常见指令。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ..llm import LLMError, LLMRouter
from .models import (
    DateRange,
    Duration,
    Filters,
    Platform,
    Query,
    Rank,
    RankBy,
    Target,
    TargetType,
)

# ---------- 规则层：确定性提取 ----------

_PLATFORM_HINTS: dict[Platform, tuple[str, ...]] = {
    Platform.BILIBILI: ("b站", "B站", "哔哩", "bilibili", "up主", "UP主"),
    Platform.ZHIHU: ("知乎", "zhihu", "专栏", "回答"),
    Platform.WECHAT: ("公众号", "微信"),
    Platform.XHS: ("小红书", "xhs", "笔记"),
}

_RANK_HINTS: dict[RankBy, tuple[str, ...]] = {
    RankBy.FAVORITES: ("收藏",),
    RankBy.LIKES: ("点赞", "赞同", "最赞"),
    RankBy.COMMENTS: ("评论", "讨论"),
    RankBy.PLAY: ("播放", "观看", "热门", "最热"),
    RankBy.DATE: ("最新", "最近发布", "按时间", "近期"),
    # 「搜索排名」「综合排序」指平台自己的默认排序（B 站是 totalrank）。
    # 这与播放量排序是**不同的结果**：用户说「搜 X 排名第一」时，
    # 想要的是搜索框里第一条，不是播放量最高那条。
    RankBy.RELEVANCE: ("排名", "综合排序", "综合排", "相关度", "最相关", "默认排序"),
}

# 「最近」既能表时间范围也能表排序，必须分开处理：
#   「最近半年 播放量前5」→ 时间范围是最近半年，排序按播放量
#   「最近 3 个视频」    → 排序按时间倒序取 3 个
# 判据：「最近」后面跟时间单位（天/周/月/年）就是范围修饰，
# 跟数量单位（个/条/篇/部/期）或直接跟内容词就是时间排序。
# 搞错的后果很实际——用户要最新 3 个，却拿到播放量最高的 3 个，完全是另一批视频。
_RECENCY_SORT_RE = re.compile(
    r"(?:最近|最新|近期)\s*"
    r"(?:[0-9一两二三四五六七八九十]+\s*(?:个|条|篇|部|期|支)?\s*)?"
    r"(?:视频|内容|作品|投稿|文章|笔记|更新|的)"
)
# 「最近N天/周/月/年」——这里的「最近」只管时间范围，不该影响排序
_RECENCY_RANGE_RE = re.compile(
    r"(?:最近|近|过去)\s*[0-9一两二三四五六七八九十半]+\s*(?:天|周|个?月|年)"
)

_CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "半": 6,  # "半年" → 6 个月
}

_TOPIC_STOPWORDS = {"方向", "相关", "领域", "类", "的", "内容", "视频", "文章"}

# 常见主题词表。优先匹配这些，避免把博主名吞进主题
# （如「影视飓风摄影方向」应得 ["摄影"] 而非 ["影视飓风摄影"]）。
_KNOWN_TOPICS = (
    "摄影", "美食", "科技", "数码", "编程", "财经", "理财", "投资", "健身",
    "旅行", "旅游", "音乐", "电影", "影视", "游戏", "教育", "考研", "职场",
    "心理", "医学", "健康", "汽车", "户外", "宠物", "穿搭", "美妆", "家居",
    "历史", "法律", "哲学", "英语", "留学", "育儿", "装修", "数学", "物理",
    "人工智能", "机器学习", "深度学习", "算法", "设计", "绘画", "写作",
)


def _to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    return _CN_NUM.get(text)


def extract_date_range(text: str, today: date | None = None) -> tuple[DateRange, list[str]]:
    """提取时间范围。

    刻意用规则而非 LLM：LLM 计算相对日期（"最近半年"）经常算错，
    而这类错误会直接导致抓错内容。
    """
    today = today or date.today()
    notes: list[str] = []

    # 最近 N 天/周/月/年、近半年
    m = re.search(r"(?:最近|近|过去)\s*([0-9一两二三四五六七八九十半]+)\s*(天|周|个?月|年)", text)
    if m:
        n = _to_int(m.group(1)) or 1
        unit = m.group(2)
        days = {"天": 1, "周": 7, "月": 30, "个月": 30, "年": 365}.get(unit, 30)
        # "半年" 特判：_CN_NUM 把"半"映射为 6，配合"年"应为 6 个月
        if m.group(1) == "半" and "年" in unit:
            delta = timedelta(days=180)
        else:
            delta = timedelta(days=n * days)
        return DateRange(from_date=today - delta), notes

    if "今年" in text:
        return DateRange(from_date=date(today.year, 1, 1)), notes
    if "去年" in text:
        return DateRange(
            from_date=date(today.year - 1, 1, 1), to_date=date(today.year - 1, 12, 31)
        ), notes

    m = re.search(r"(20\d{2})\s*年(?:\s*(\d{1,2})\s*月)?", text)
    if m:
        year = int(m.group(1))
        if m.group(2):
            month = int(m.group(2))
            start = date(year, month, 1)
            end_month_start = (
                date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            )
            return DateRange(from_date=start, to_date=end_month_start - timedelta(days=1)), notes
        return DateRange(from_date=date(year, 1, 1), to_date=date(year, 12, 31)), notes

    return DateRange(), notes


def extract_limit(text: str) -> int | None:
    """提取数量：前 20、Top100、20 条。

    注意：必须先剔除时间短语，否则「最近3个月...的10条」会把 3 当成数量。
    这类错误会静默导致抓错数量的内容。
    """
    cleaned = re.sub(
        r"(?:最近|近|过去)\s*[0-9一两二三四五六七八九十半]+\s*(?:天|周|个?月|年)",
        " ",
        text,
    )
    cleaned = re.sub(r"20\d{2}\s*年", " ", cleaned)

    m = re.search(r"(?:前|top|Top|TOP)\s*([0-9]{1,4})", cleaned)
    if m:
        return int(m.group(1))
    m = re.search(r"([0-9]{1,4})\s*(?:条|篇|个|部)", cleaned)
    if m:
        return int(m.group(1))
    m = re.search(r"前\s*([一两二三四五六七八九十]+)", cleaned)
    if m:
        return _to_int(m.group(1))
    # 「排名第一」「第一个」「第 1 条」→ 1。
    # 「第N」是很自然的说法，此前完全没识别，会静默取默认 20 条——
    # 用户只想看一个视频，结果抓了 20 个并产生 20 倍成本。
    m = re.search(r"第\s*([0-9]{1,3}|[一两二三四五六七八九十]+)\s*(?:个|条|篇|部|名|位)?", cleaned)
    if m:
        token = m.group(1)
        return int(token) if token.isdigit() else _to_int(token)
    return None


def extract_duration(text: str) -> Duration:
    """提取时长要求。默认过滤短视频切片的逻辑放在 planner，不在此硬编码。"""
    m = re.search(r"(?:超过|大于|长于|>)\s*([0-9]+)\s*分钟", text)
    if m:
        return Duration(min_sec=int(m.group(1)) * 60)
    m = re.search(r"(?:少于|小于|短于|<)\s*([0-9]+)\s*分钟", text)
    if m:
        return Duration(max_sec=int(m.group(1)) * 60)
    if "长视频" in text:
        return Duration(min_sec=600)
    if "短视频" in text:
        return Duration(max_sec=300)
    return Duration()


def detect_platform(text: str) -> Platform:
    for platform, hints in _PLATFORM_HINTS.items():
        if any(h in text for h in hints):
            return platform
    return Platform.UNKNOWN


def detect_rank(text: str) -> RankBy | None:
    """识别排序方式。

    显式排序词（播放量/收藏等）优先于「最近」这类时间暗示——
    「最近半年播放量前5」应按播放量排，「最近3个视频」才按时间排。
    """
    for rank, hints in _RANK_HINTS.items():
        if any(h in text for h in hints):
            return rank

    # 没有显式排序词时，判断「最近」是否表达时间倒序
    if _RECENCY_SORT_RE.search(text):
        # 排除「最近半年的视频」这类——它同时含范围与内容词，
        # 范围已由 extract_date_range 处理，排序应留给默认值
        stripped = _RECENCY_RANGE_RE.sub(" ", text)
        if _RECENCY_SORT_RE.search(stripped):
            return RankBy.DATE
    return None


def extract_series(text: str) -> list[str]:
    """提取系列名/标题必含词。

    UP 主常把内容组织成系列（「每天一个宝藏问题」「XX 系列」），
    标题里带固定标记。这类词应当作**标题过滤条件**而非语义主题：
    要的是「标题含这几个字的那批视频」，不是「语义上相关的视频」。

    识别三种写法，都是用户表达系列的明确信号：
      ①「【每天一个宝藏问题】」「《XX》」—— 书名号/方括号内
      ②「每天一个宝藏问题 系列」    —— 显式带「系列」二字
      ③「XX合集」                   —— 合集后缀
    """
    found: list[str] = []

    for m in re.finditer(r"[【《\[]([^】》\]]{2,30})[】》\]]", text):
        found.append(m.group(1).strip())

    # 「X系列」「X合集」：取紧邻的 3-15 字作为系列名。
    # 下限设 3 是为了避开「这个系列」「该合集」这类指代。
    for m in re.finditer(
        r"([\u4e00-\u9fffA-Za-z0-9的]{3,20})\s*(?:系列|合集|这个系列)", text
    ):
        name = m.group(1).strip()
        # 「海安雨的每天一个宝藏问题系列」——「的」之后才是系列名。
        # 不切断会把 UP 主名吞进系列名，导致标题过滤一条都匹配不上。
        if "的" in name:
            name = name.rsplit("的", 1)[-1].strip()
        # 剥掉粘在前面的平台词与 UP 主提示词
        name = re.sub(r"^(?:b站|B站|哔哩|bilibili|up主|UP主|博主)", "", name).strip()
        if len(name) >= 3:
            found.append(name)

    # 去重保序
    out: list[str] = []
    for f in found:
        if f and f not in out:
            out.append(f)
    return out


def detect_target_type(text: str) -> TargetType:
    if re.search(r"https?://", text):
        return TargetType.URL
    if any(k in text for k in ("我的收藏", "收藏夹里", "我收藏")):
        return TargetType.FAVORITES
    if "专栏" in text:
        return TargetType.COLUMN

    # 「关于X」「搜索X」「有关X」是明确的关键词检索意图，
    # 优先于「的视频」这类弱信号——否则「B站关于RAG的视频」会被误判成找 UP 主，
    # 进而走消歧流程搜不到人而失败。
    if re.search(r"(关于|有关|搜索|检索|搜一下)\s*[\u4e00-\u9fffA-Za-z0-9]", text):
        return TargetType.KEYWORD

    if any(k in text for k in ("up主", "UP主", "博主", "作者", "频道", "的投稿")):
        return TargetType.UPLOADER
    # 「X的视频」需排除「关于…的视频」已在上面处理
    if re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2,20}的(?:视频|笔记|文章)", text):
        return TargetType.UPLOADER
    # 「X最近/最新 N 个视频」也是要某人的投稿，不是全站搜关键词。
    # 这两者结果完全不同：搜关键词会返回一堆别人讲该话题的视频。
    # 用「时间倒序 + 明确条数」作为判据——全站搜索很少这么表达。
    if _RECENCY_SORT_RE.search(text) and re.search(
        r"[0-9一两二三四五六七八九十]+\s*(?:个|条|篇|部|期|支)", text
    ):
        return TargetType.UPLOADER
    return TargetType.KEYWORD


def extract_topic(text: str) -> list[str]:
    """提取主题方向，如「摄影方向」→ ["摄影"]。

    难点：「影视飓风摄影方向」中「影视飓风」是博主名，不能吞进主题。
    策略：先在「方向」前的片段里找已知主题词（最长优先），
    找不到才退化为取紧邻的 2-4 字。
    """
    topics: list[str] = []
    for m in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9]{2,20})\s*(?:方向|相关|领域|类)", text):
        chunk = m.group(1)

        # 已知主题词优先，按长度降序以便「机器学习」胜过「学习」
        matched = [t for t in _KNOWN_TOPICS if t in chunk]
        if matched:
            topics.append(max(matched, key=len))
            continue

        # 退化：取末尾 2-4 字，并剥掉粘连的虚词
        tail = chunk[-4:]
        tail = re.sub(r"^(?:的|在|讲|年|月|和|与|及)+", "", tail)
        if len(tail) >= 2 and tail not in _TOPIC_STOPWORDS:
            topics.append(tail)

    return list(dict.fromkeys(topics))


# ---------- LLM 层：语义补全 ----------

_PARSE_SYSTEM = """你把用户的中文检索指令解析成 JSON。只输出 JSON，不要任何解释。

规则：
- 只提取用户**明确表达**的信息，不要推测补充。
- target_name 是博主名/专栏名/搜索关键词的**原始文字**，不要翻译或改写。
- 无法确定的字段填 null 或空数组。
- 不要计算日期，日期由程序处理。"""

_PARSE_TEMPLATE = """解析这条指令：{text}

输出 JSON：
{{
  "intent": "discover 或 single",
  "platform": "bilibili|zhihu|wechat|xhs|unknown",
  "target_type": "uploader|column|keyword|favorites",
  "target_name": "博主名/专栏名/关键词的原文，没有则 null",
  "topics": ["主题方向关键词，如 摄影、美食"],
  "rank_by": "play|favorites|likes|comments|date|relevance",
  "limit": 数字或 null,
  "exclude_keywords": ["要排除的内容类型，如 直播回放、带货"],
  "ambiguous_target": "若博主名指代不清则填说明，否则 null"
}}"""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            return {}
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        return {}
    return {}


class QueryParser:
    def __init__(self, router: LLMRouter | None = None) -> None:
        self.router = router

    def parse(self, text: str, *, today: date | None = None) -> Query:
        text = (text or "").strip()
        query = Query(raw_input=text)

        # ---- 规则层（确定性，优先） ----
        date_range, notes = extract_date_range(text, today)
        query.filters.date_range = date_range
        query.assumptions.extend(notes)

        query.filters.duration = extract_duration(text)
        query.filters.topic = extract_topic(text)

        # 系列名作为标题过滤条件。注意与 topic 的区别：
        # topic 是语义方向（走 LLM 打分），series 是标题必含词（精确匹配）。
        # 用户说「每天一个宝藏问题系列」时要的是后者。
        series = extract_series(text)
        if series:
            query.filters.include_keywords = series

        rule_platform = detect_platform(text)
        rule_type = detect_target_type(text)
        rule_rank = detect_rank(text)
        rule_limit = extract_limit(text)

        if rule_rank:
            query.rank = Rank(by=rule_rank)
        if rule_limit:
            query.limit = rule_limit

        # ---- LLM 层（语义补全） ----
        llm_data = self._llm_parse(text)

        target = Target(
            platform=rule_platform,
            type=rule_type,
            raw=(llm_data.get("target_name") or "").strip() or self._guess_name(text),
        )
        if target.platform is Platform.UNKNOWN:
            target.platform = self._coerce_platform(llm_data.get("platform"))
        if llm_data.get("target_type") and rule_type is TargetType.KEYWORD:
            target.type = self._coerce_type(llm_data["target_type"], rule_type)
        query.targets = [target]

        if not query.filters.topic and llm_data.get("topics"):
            query.filters.topic = [
                str(t).strip() for t in llm_data["topics"] if str(t).strip()
            ]
        if not rule_rank and llm_data.get("rank_by"):
            query.rank = Rank(by=self._coerce_rank(llm_data["rank_by"]))
        if not rule_limit and isinstance(llm_data.get("limit"), int):
            query.limit = llm_data["limit"]
        if llm_data.get("exclude_keywords"):
            query.filters.exclude_keywords = [
                str(k).strip() for k in llm_data["exclude_keywords"] if str(k).strip()
            ]
        if llm_data.get("intent") == "single":
            query.intent = "single"

        # 关键词搜索且用户没指定排序时，用平台综合排序而非播放量。
        #
        # 「B站搜 ai infra」的自然语义是「搜索框里的默认结果」，
        # 而按播放量排会给出**另一个视频**——通常是某个爆款泛科普，
        # 而不是搜索引擎认为最相关的那条。这个差异用户看不见，
        # 只会觉得「工具给的不是我要的」。
        # UP 主投稿场景保持播放量默认（找某人最受欢迎的作品是合理默认）。
        if (
            not rule_rank
            and not llm_data.get("rank_by")
            and query.targets
            and query.targets[0].type is TargetType.KEYWORD
        ):
            query.rank = Rank(by=RankBy.RELEVANCE)
            query.assumptions.append("未指定排序，关键词搜索默认用平台综合排序")

        self._apply_defaults(query, had_limit=bool(rule_limit or llm_data.get("limit")))
        return query

    def _llm_parse(self, text: str) -> dict:
        if self.router is None:
            return {}
        try:
            resp = self.router.complete(
                "map",
                _PARSE_TEMPLATE.format(text=text),
                system=_PARSE_SYSTEM,
                json_mode=True,
            )
            return _parse_json(resp.text)
        except LLMError:
            # 无可用 LLM 时退化为纯规则解析，不阻断流程
            return {}

    @staticmethod
    def _guess_name(text: str) -> str:
        """无 LLM 时的兜底：从指令中剥离修饰语，取剩余主体作为目标名。"""
        if m := re.search(r"[「『\"'“]([^」』\"'”]{2,30})[」』\"'”]", text):
            return m.group(1).strip()
        # 「关于X的视频」「搜索X前10」→ X
        #
        # 英文词组内部有空格是常态（ai infra、vector database、rust async），
        # 因此英文分支显式允许空格连接，遇到中文或修饰词才停止。
        # 此前字符类不含空格且 lookahead 含 \s，导致「ai infra」被截成「ai」——
        # 搜 ai 和搜 ai infra 是完全不同的结果，而且不会报错，只会静默搜错。
        if m := re.search(
            r"(?:关于|有关|搜索|检索|搜一下)\s*"
            r"([A-Za-z0-9]+(?:[ \-_][A-Za-z0-9]+){0,4}"
            r"|[\u4e00-\u9fffA-Za-z0-9]{2,20}?)"
            r"(?=\s*(?:的|前|top|Top|TOP|收藏|播放|点赞|评论|排名|综合|最|第"
            r"|[\u4e00-\u9fff]|[0-9]|$))",
            text,
        ):
            if name := m.group(1).strip():
                return name
        if m := re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,20})的(?:视频|投稿|专栏|文章|笔记)", text):
            # 这里必须再过一遍清洗：这个分支曾直接 return，
            # 导致「B站影视飓风最近的视频」提取出「B站影视飓风最近」——
            # 平台词与时间修饰全粘在名字里，搜索必然查不到人。
            name = QueryParser._strip_modifiers(m.group(1), full_text=text)
            if name:
                return name

        return QueryParser._strip_modifiers(text, full_text=text)

    @staticmethod
    def _strip_modifiers(chunk: str, *, full_text: str) -> str:
        """剥掉平台词、时间短语、排序与数量修饰，留下目标主体。

        full_text 用于提取主题词——主题可能出现在 chunk 之外，
        但同样不该混进名称。
        """
        cleaned = chunk
        # 先去时间短语，避免残留数字
        cleaned = re.sub(
            r"(?:最近|近|过去)\s*[0-9一两二三四五六七八九十半]+\s*(?:天|周|个?月|年)",
            " ", cleaned,
        )
        cleaned = re.sub(r"20\d{2}\s*年(?:\s*\d{1,2}\s*月)?", " ", cleaned)
        # 去系列名。系列名另有 include_keywords 承载，
        # 留在这里会让 UP 主名变成「海安雨每天一个宝藏问题」而搜不到人。
        for s in extract_series(full_text):
            cleaned = cleaned.replace(s, " ")
        cleaned = re.sub(r"[【】《》\[\]]", " ", cleaned)
        # 去平台词
        for hints in _PLATFORM_HINTS.values():
            for h in hints:
                cleaned = cleaned.replace(h, " ")
        # 去主题词（主题另有字段承载，不该混进名称）
        for topic in extract_topic(full_text):
            cleaned = cleaned.replace(topic, " ")
        # 去排序与数量修饰。
        # 「最近/最新/近期」必须在这里剥掉，否则会粘进名称变成
        # 「影视飓风最近」这种查不到任何结果的关键词。
        cleaned = re.sub(
            r"(收藏量?|播放量?|点赞|赞同|评论|最新|最近|近期|最热|最高|方向|相关"
            r"|领域|关于|更新|投稿|作品|前|top|Top|TOP|[0-9]+|条|篇|个|部|期|支"
            r"|搜索|检索|搜一下|排名|综合排序?|第|名|讲了什么|讲什么|主要内容"
            r"|系列|合集|专门讲|讲的|里有"
            r"|的|里|我|长视频|短视频)",
            " ", cleaned,
        )
        parts = [p for p in cleaned.split() if len(p) >= 2]
        if not parts:
            return ""
        # 连续的英文 token 合并成词组：英文名称本身常含空格
        # （ai infra、Two Minute Papers），只取 parts[0] 会把它们截断。
        if re.fullmatch(r"[A-Za-z0-9]+", parts[0]):
            merged = [parts[0]]
            for p in parts[1:]:
                if not re.fullmatch(r"[A-Za-z0-9]+", p):
                    break
                merged.append(p)
            return " ".join(merged)
        return parts[0]

    @staticmethod
    def _coerce_platform(value: object) -> Platform:
        try:
            return Platform(str(value))
        except ValueError:
            return Platform.UNKNOWN

    @staticmethod
    def _coerce_type(value: object, default: TargetType) -> TargetType:
        try:
            return TargetType(str(value))
        except ValueError:
            return default

    @staticmethod
    def _coerce_rank(value: object) -> RankBy:
        try:
            return RankBy(str(value))
        except ValueError:
            return RankBy.PLAY

    @staticmethod
    def _apply_defaults(query: Query, *, had_limit: bool) -> None:
        """补默认值，并把每个默认决定写进 assumptions。

        这是"自动决定必须可见"原则的落地：不能悄悄默认。
        """
        if not had_limit:
            query.assumptions.append(f"未指定数量，默认取前 {query.limit} 条")
        if not query.filters.date_range.from_date and not query.filters.date_range.to_date:
            query.assumptions.append("未指定时间范围，默认不限时间")
        if query.limit > 100:
            query.assumptions.append(
                f"请求数量较大（{query.limit}），执行前会预演成本"
            )

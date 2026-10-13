"""歧义检测与反问策略。

**核心原则：只在"猜错代价高"时才阻断提问。**

反问做过头非常烦人；但静默默认又会烧掉大量算力或抓错内容。
折中点是「自动决定 + 全部可见」：
- 能推断的就推断，并写进 `assumptions` 展示给用户；
- 只有以下四类才阻断：实体不唯一、语义真歧义、量级超预算、指令明显不合理。

`--yes` 可跳过所有非阻断确认，用于脚本化与定时任务。
"""

from __future__ import annotations

from .models import Platform, Query, Question, RankBy, TargetType

# 平台能力矩阵：哪些平台支持按哪些指标原生排序。
# 不支持的会由 ranker 本地补偿，并明确告知口径（不伪装精确）。
NATIVE_RANK_SUPPORT: dict[Platform, set[RankBy]] = {
    Platform.BILIBILI: {RankBy.PLAY, RankBy.DATE, RankBy.COMMENTS, RankBy.RELEVANCE},
    Platform.ZHIHU: {RankBy.DATE, RankBy.LIKES, RankBy.FAVORITES, RankBy.RELEVANCE},
    Platform.XHS: {RankBy.RELEVANCE, RankBy.DATE},
    Platform.WECHAT: set(),
}


def detect_issues(query: Query) -> Query:
    """检测歧义与能力落差，填充 assumptions 与 blocking_questions。

    就地修改并返回同一个 Query，便于链式调用。
    """
    if not query.targets:
        query.blocking_questions.append(
            Question(
                key="target",
                text="没能识别出要检索的对象。请明确说明博主名、专栏名或搜索关键词。",
            )
        )
        return query

    target = query.targets[0]

    # ---- 1. 目标名缺失：阻断 ----
    if not target.raw and target.type is not TargetType.FAVORITES:
        query.blocking_questions.append(
            Question(
                key="target_name",
                text="没能识别出具体的博主/专栏/关键词名称，请补充说明。",
            )
        )

    # ---- 2. 平台未知：阻断（不同平台能力差异极大，猜错等于白跑） ----
    if target.platform is Platform.UNKNOWN:
        query.blocking_questions.append(
            Question(
                key="platform",
                text=f"「{target.raw or '目标'}」在哪个平台？",
                options=["B 站", "知乎", "微信公众号", "小红书"],
            )
        )

    # ---- 3. 「收藏量」的真歧义：阻断 ----
    # ①该内容被收藏数排序 ②我的收藏夹里的内容——两者结果完全不同
    if query.rank.by is RankBy.FAVORITES and target.type is not TargetType.FAVORITES:
        raw = query.raw_input
        if any(k in raw for k in ("我的收藏", "我收藏", "收藏夹")):
            target.type = TargetType.FAVORITES
            query.assumptions.append("理解为「我的收藏夹」中的内容")
        else:
            query.blocking_questions.append(
                Question(
                    key="favorites_meaning",
                    text="「收藏量」指的是哪种？",
                    options=[
                        "该内容被收藏的次数（按被收藏数排序）",
                        "我自己的收藏夹里的内容",
                    ],
                )
            )

    # ---- 4. 公众号无批量发现接口：阻断并说明这是平台硬限制 ----
    if target.platform is Platform.WECHAT and target.type is not TargetType.URL:
        query.blocking_questions.append(
            Question(
                key="wechat_limit",
                text=(
                    "微信公众号没有公开的「某号全部文章」接口，无法批量发现。"
                    "可以：① 直接提供文章链接逐篇消化；② 若有该号的 RSS 源可提供。"
                ),
            )
        )

    # ---- 5. 平台不支持该排序：不阻断，告知并本地补偿 ----
    supported = NATIVE_RANK_SUPPORT.get(target.platform, set())
    if supported and query.rank.by not in supported:
        query.assumptions.append(
            f"{_platform_name(target.platform)}不支持按「{_rank_name(query.rank.by)}」"
            f"原生检索，将拉取更大候选池后本地补齐该指标再排序"
            f"（结果为近似值，会标注口径）"
        )

    # ---- 6. 主题方向需语义判定：不阻断，告知置信度机制 ----
    if query.filters.topic:
        query.assumptions.append(
            f"主题「{'、'.join(query.filters.topic)}」将通过标签与语义判定，"
            f"结果会标注置信度（可用 --strict 提高门槛）"
        )

    # ---- 7. 数量过大：不在此阻断，交给 planner 的成本预演处理 ----
    if query.limit > 200:
        query.assumptions.append(
            f"数量较大（{query.limit}），将按平台限流分批拉取，耗时较长"
        )

    return query


def _platform_name(platform: Platform) -> str:
    return {
        Platform.BILIBILI: "B 站",
        Platform.ZHIHU: "知乎",
        Platform.WECHAT: "微信公众号",
        Platform.XHS: "小红书",
    }.get(platform, platform.value)


def _rank_name(rank: RankBy) -> str:
    return {
        RankBy.PLAY: "播放量",
        RankBy.FAVORITES: "收藏量",
        RankBy.LIKES: "点赞数",
        RankBy.COMMENTS: "评论数",
        RankBy.DATE: "发布时间",
        RankBy.RELEVANCE: "相关度",
    }.get(rank, rank.value)


def apply_answers(query: Query, answers: dict[str, str]) -> Query:
    """把用户对反问的回答写回 Query，并清空已解决的问题。"""
    remaining: list[Question] = []

    for question in query.blocking_questions:
        answer = answers.get(question.key)
        if not answer:
            remaining.append(question)
            continue

        if question.key == "platform":
            mapping = {
                "b 站": Platform.BILIBILI, "b站": Platform.BILIBILI,
                "bilibili": Platform.BILIBILI, "1": Platform.BILIBILI,
                "知乎": Platform.ZHIHU, "zhihu": Platform.ZHIHU, "2": Platform.ZHIHU,
                "微信公众号": Platform.WECHAT, "公众号": Platform.WECHAT, "3": Platform.WECHAT,
                "小红书": Platform.XHS, "xhs": Platform.XHS, "4": Platform.XHS,
            }
            picked = mapping.get(answer.strip().lower())
            if picked and query.targets:
                query.targets[0].platform = picked
                query.assumptions.append(f"平台已确认为 {_platform_name(picked)}")
            else:
                remaining.append(question)

        elif question.key == "favorites_meaning":
            if answer.strip().startswith(("2", "我")):
                if query.targets:
                    query.targets[0].type = TargetType.FAVORITES
                query.assumptions.append("已确认为「我的收藏夹」")
            else:
                query.assumptions.append("已确认为「按被收藏数排序」")

        elif question.key in ("target_name", "target"):
            if query.targets:
                query.targets[0].raw = answer.strip()
            else:
                from .models import Target

                query.targets = [Target(raw=answer.strip())]
            query.assumptions.append(f"目标已确认为「{answer.strip()}」")

        else:
            # 其他问题（如公众号限制）由用户改用别的方式，视为已知悉
            query.assumptions.append(f"{question.key}: {answer.strip()}")

    query.blocking_questions = remaining
    return query

"""领域分区。

**为什么需要**：库里会混着理财、技术、摄影。理财 agent 不该被摄影内容干扰。
每条 claim 与 source 带 domain 标签，检索时物理隔离。

领域可配专属规则——理财领域尤其需要：
自媒体观点权重要降、必须带时间标注、结论半年就可能失效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 内置领域规则。用户可在 config.yaml 的 domains 段覆盖。
BUILTIN_DOMAINS: dict[str, dict] = {
    "finance": {
        "keywords": [
            "理财", "基金", "股票", "债券", "利率", "资产配置", "保险", "定投",
            "收益率", "通胀", "美债", "存款", "投资", "股市", "A股", "港股",
            "美股", "ETF", "可转债", "REITs", "货币基金", "净值", "估值",
            "市盈率", "分红", "杠杆", "止损", "仓位", "复利", "年化",
        ],
        "extract_numeric": True,
        "require_as_of": True,
        "default_max_staleness_days": 180,
        "disclaimer": True,
        "authority_weights": {
            "official": 1.0,
            "peer_reviewed": 0.9,
            "media": 0.6,
            "self_media": 0.4,
            "anonymous": 0.2,
        },
    },
    "tech": {
        "keywords": [
            "编程", "代码", "算法", "框架", "模型", "训练", "推理", "部署",
            "数据库", "API", "架构", "重构", "性能", "并发", "缓存",
            "RAG", "LLM", "Transformer", "微调", "向量", "检索", "embedding",
        ],
        "extract_numeric": False,
        "require_as_of": False,
        "default_max_staleness_days": 730,
        "disclaimer": False,
    },
    "photography": {
        "keywords": [
            "摄影", "镜头", "构图", "布光", "相机", "调色", "后期",
            "光圈", "快门", "ISO", "人像", "风光", "胶片", "修图",
        ],
        "extract_numeric": False,
        "require_as_of": False,
        "default_max_staleness_days": 1825,
        "disclaimer": False,
    },
}


@dataclass
class DomainRule:
    name: str
    keywords: list[str] = field(default_factory=list)
    extract_numeric: bool = False
    require_as_of: bool = False
    default_max_staleness_days: int = 365
    disclaimer: bool = False
    authority_weights: dict[str, float] = field(default_factory=dict)

    def score(self, text: str) -> float:
        """该内容属于本领域的强度。

        **单个关键词不足以判定归属**——「聊聊摄影之外的话题」只命中「摄影」
        一次，若判为摄影领域，理财 agent 就会检索到不相关内容。
        因此要求至少 2 个不同关键词命中才给出可用分数。
        """
        if not self.keywords:
            return 0.0
        hits = sum(1 for kw in self.keywords if _contains(text, kw))
        if hits < 2:
            return 0.0
        # 2 个命中给 0.35，3 个以上基本可确定
        return min(1.0, 0.35 + 0.22 * (hits - 2))


def _contains(text: str, keyword: str) -> bool:
    """中文直接子串；英文加词边界避免误匹配。"""
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE) is not None


class DomainClassifier:
    def __init__(self, custom: dict[str, dict] | None = None) -> None:
        merged = {k: dict(v) for k, v in BUILTIN_DOMAINS.items()}
        for name, spec in (custom or {}).items():
            if name in merged:
                merged[name].update(spec)
                # 关键词合并而非覆盖，保留内置词表
                merged[name]["keywords"] = list(
                    dict.fromkeys(
                        BUILTIN_DOMAINS.get(name, {}).get("keywords", [])
                        + list(spec.get("keywords", []))
                    )
                )
            else:
                merged[name] = dict(spec)

        self.rules: dict[str, DomainRule] = {}
        for name, spec in merged.items():
            self.rules[name] = DomainRule(
                name=name,
                keywords=[str(k) for k in spec.get("keywords", [])],
                extract_numeric=bool(spec.get("extract_numeric", False)),
                require_as_of=bool(spec.get("require_as_of", False)),
                default_max_staleness_days=int(
                    spec.get("default_max_staleness_days", 365)
                ),
                disclaimer=bool(spec.get("disclaimer", False)),
                authority_weights=dict(spec.get("authority_weights") or {}),
            )

    def classify(self, text: str, *, threshold: float = 0.3) -> list[tuple[str, float]]:
        """判定内容所属领域。可多归属（如技术+理财的量化内容）。"""
        scored = [
            (name, rule.score(text)) for name, rule in self.rules.items()
        ]
        picked = [(n, s) for n, s in scored if s >= threshold]
        picked.sort(key=lambda x: -x[1])
        return picked

    def primary(self, text: str) -> str | None:
        picked = self.classify(text)
        return picked[0][0] if picked else None

    def rule(self, name: str) -> DomainRule | None:
        return self.rules.get(name)

    def names(self) -> list[str]:
        return list(self.rules)

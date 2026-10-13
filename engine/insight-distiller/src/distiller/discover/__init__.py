"""自然语言选材（M2.5）。

把「某 UP 主摄影方向最近半年播放量前 20」这类人话，
变成可执行的候选清单，并在执行前预演成本。
"""

from .engine import DiscoveryEngine, DiscoveryPlan
from .models import Candidate, Platform, Query, RankBy, Target, TargetType
from .planner import Estimate, estimate
from .providers import DiscoveryError, list_providers
from .topic import TopicFilter, load_user_topics

__all__ = [
    "Candidate",
    "DiscoveryEngine",
    "DiscoveryError",
    "DiscoveryPlan",
    "Estimate",
    "Platform",
    "Query",
    "RankBy",
    "Target",
    "TargetType",
    "TopicFilter",
    "estimate",
    "list_providers",
    "load_user_topics",
]

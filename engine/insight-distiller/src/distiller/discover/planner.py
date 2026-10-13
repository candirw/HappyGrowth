"""成本与耗时预演（dry-run）。

**为什么必须有**：一句「某 UP 主全部视频」可能炸出 280 条，
其中 190 条无字幕要走 ASR ≈ 9 小时算力。必须先算清楚再问用户要不要跑。

预估基于实测数据：
- 有字幕：仅需 LLM 浓缩，约 3–8 秒
- 无字幕：需下载 + Whisper 转写，M 系列芯片约为音频时长的 1/12
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from .models import Candidate

# 实测经验值
_ASR_SPEED_RATIO = 1 / 12       # 转写耗时 ≈ 音频时长 / 12（Apple Silicon）
_DOWNLOAD_SEC_PER_MIN = 1.5     # 每分钟音频的下载耗时
_DISTILL_SEC_PER_1K_CHARS = 1.2 # 每千字浓缩耗时
_CHARS_PER_MINUTE_SPEECH = 220  # 中文口播每分钟字数

# token 估算：中文约 1.5 字/token
_TOKENS_PER_CHAR = 1 / 1.5
# map + reduce 大致会把原文过一遍多一点
_TOKEN_MULTIPLIER = 1.3
_OUTPUT_TOKEN_RATIO = 0.12


@dataclass
class Estimate:
    total: int = 0
    with_subtitle: int = 0
    need_asr: int = 0
    # 字幕状态未探测的条数。必须与「确认无字幕」分开统计——
    # 否则展示成「有字幕 0 条」会让用户以为确实没字幕，
    # 而实际上多数 B 站视频有 CC 字幕，真实成本远低于预估。
    subtitle_unknown: int = 0
    already_in_library: int = 0
    total_duration_sec: int = 0
    est_chars: int = 0
    est_seconds: float = 0.0
    est_cost_usd: float = 0.0
    topic_llm_calls: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def to_process(self) -> int:
        return self.total - self.already_in_library

    def human_time(self) -> str:
        s = int(self.est_seconds)
        if s < 60:
            return f"{s} 秒"
        if s < 3600:
            return f"{s // 60} 分 {s % 60} 秒"
        return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


def estimate(
    candidates: list[Candidate],
    config: Config,
    *,
    topic_llm_calls: int = 0,
) -> Estimate:
    """预演批量消化的耗时与成本。"""
    est = Estimate(total=len(candidates), topic_llm_calls=topic_llm_calls)

    price_in = 0.0
    price_out = 0.0
    try:
        task = config.task("reduce")
        price = config.providers[task.provider].price_per_1m
        price_in, price_out = price.input, price.output
    except Exception:  # noqa: BLE001 - 无可用 provider 时按 0 计
        est.warnings.append("未配置可用的 LLM，成本按 0 估算")

    for cand in candidates:
        if cand.in_library:
            est.already_in_library += 1
            continue

        duration = cand.duration_sec or 0
        est.total_duration_sec += duration

        if cand.has_subtitle:
            est.with_subtitle += 1
            chars = int(duration / 60 * _CHARS_PER_MINUTE_SPEECH) if duration else 3000
        elif cand.has_subtitle is False:
            est.need_asr += 1
            minutes = duration / 60 if duration else 10
            est.est_seconds += minutes * 60 * _ASR_SPEED_RATIO
            est.est_seconds += minutes * _DOWNLOAD_SEC_PER_MIN
            chars = int(minutes * _CHARS_PER_MINUTE_SPEECH)
        else:
            # 字幕情况未知（发现阶段不探测，避免为每条候选多发一次请求）：
            # 按一半概率需要 ASR 保守估算
            est.need_asr += 1
            est.subtitle_unknown += 1
            minutes = duration / 60 if duration else 10
            est.est_seconds += minutes * 60 * _ASR_SPEED_RATIO * 0.5
            chars = int(minutes * _CHARS_PER_MINUTE_SPEECH) if duration else 3000

        est.est_chars += chars
        est.est_seconds += chars / 1000 * _DISTILL_SEC_PER_1K_CHARS

    tokens_in = int(est.est_chars * _TOKENS_PER_CHAR * _TOKEN_MULTIPLIER)
    tokens_out = int(tokens_in * _OUTPUT_TOKEN_RATIO)
    est.est_cost_usd = (tokens_in * price_in + tokens_out * price_out) / 1_000_000

    if topic_llm_calls:
        est.est_cost_usd += topic_llm_calls * 0.0006  # 主题判定的批量调用
        est.est_seconds += topic_llm_calls * 1.5

    # 预算护栏
    budget = config.limits.monthly_budget_usd
    if est.est_cost_usd > budget:
        est.warnings.append(
            f"预估成本 ${est.est_cost_usd:.2f} 超过月度预算 ${budget}，"
            f"建议减少数量或用 --profile local 走本地模型"
        )
    elif est.est_cost_usd > config.limits.warn_per_task_usd:
        est.warnings.append(f"本次预估花费 ${est.est_cost_usd:.2f}")

    if est.need_asr and est.est_seconds > 1800:
        est.warnings.append(
            f"{est.need_asr} 条需要音频转写，预计耗时 {est.human_time()}。"
            f"建议先用 --limit 缩小范围试跑"
        )

    if est.already_in_library:
        est.warnings.append(
            f"{est.already_in_library} 条已在库中，将跳过（--force 可重跑）"
        )

    return est

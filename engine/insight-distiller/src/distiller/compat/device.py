"""计算设备探测：cuda -> mps -> cpu。

M2 的 ASR 与 embedding 会用到。基准实现（faster-whisper / CTranslate2）
三平台通用，此处只决定加速后端，不改变功能。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    device: str        # cuda | mps | cpu
    compute_type: str  # 建议的 faster-whisper compute_type
    note: str


def detect_device() -> DeviceInfo:
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return DeviceInfo("cuda", "float16", "NVIDIA GPU 可用")
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return DeviceInfo("mps", "int8_float16", "Apple Silicon GPU 可用")
    except ImportError:
        pass

    if sys.platform == "darwin":
        return DeviceInfo("cpu", "int8", "未装 torch，走 CPU（Apple Silicon CPU 性能足够）")
    return DeviceInfo("cpu", "int8", "走 CPU")

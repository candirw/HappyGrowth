"""订阅自动化（M6）。

把 M2.5 的自然语言选材固化成定期任务。
"""

from .runner import RunResult, SubscriptionRunner, render_digest_markdown

__all__ = ["RunResult", "SubscriptionRunner", "render_digest_markdown"]

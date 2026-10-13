"""异步任务。

视频消化耗时可达数分钟，同步返回必然连接超时。
MCP 与 HTTP 共用这一套任务管理。

存内存而非数据库：任务状态是进程生命周期内的临时信息，
落库会带来清理、并发写、陈旧记录等一堆麻烦，收益却很低。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from ..config import Config
from .schemas import SourceSummary, TaskInfo, TaskStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskManager:
    def __init__(self, config: Config, *, profile: str | None = None, max_keep: int = 200):
        self.config = config
        self.profile = profile
        self.max_keep = max_keep
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        force: bool = False,
    ) -> TaskInfo:
        task_id = uuid.uuid4().hex[:16]
        info = TaskInfo(
            task_id=task_id,
            status=TaskStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self._tasks[task_id] = info
            self._evict_locked()

        thread = threading.Thread(
            target=self._run,
            args=(task_id, url, text, title, force),
            daemon=True,
        )
        thread.start()
        return info

    def get(self, task_id: str) -> TaskInfo | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[TaskInfo]:
        with self._lock:
            items = sorted(
                self._tasks.values(), key=lambda t: t.created_at, reverse=True
            )
        return items[:limit]

    def _evict_locked(self) -> None:
        if len(self._tasks) <= self.max_keep:
            return
        done = sorted(
            (t for t in self._tasks.values()
             if t.status in (TaskStatus.DONE, TaskStatus.FAILED)),
            key=lambda t: t.updated_at,
        )
        for task in done[: len(self._tasks) - self.max_keep]:
            self._tasks.pop(task.task_id, None)

    def _update(self, task_id: str, **fields) -> None:
        with self._lock:
            info = self._tasks.get(task_id)
            if info is None:
                return
            for key, value in fields.items():
                setattr(info, key, value)
            info.updated_at = _now()

    def _run(
        self,
        task_id: str,
        url: str | None,
        text: str | None,
        title: str | None,
        force: bool,
    ) -> None:
        from ..pipeline import Pipeline

        self._update(task_id, status=TaskStatus.RUNNING, progress="启动中")
        try:
            # Pipeline 自带连接，线程内新建即可（SQLite 连接不可跨线程）
            with Pipeline(
                self.config,
                profile=self.profile,
                progress=lambda m: self._update(task_id, progress=m),
            ) as pipe:
                raw = pipe.fetch(url) if url else pipe.from_paste(text or "", title=title)
                result = pipe.process(raw, force=force)
                source_id = int(result.source.id) if result.source.id else None

                summary = None
                if source_id:
                    from .service import AgentService

                    summary = AgentService(pipe.db, self.config).get_source(source_id)

            self._update(
                task_id,
                status=TaskStatus.DONE,
                progress="完成",
                source_id=source_id,
                result=summary,
            )
        except Exception as exc:  # noqa: BLE001 - 任务失败要记录而非崩溃线程
            self._update(task_id, status=TaskStatus.FAILED, error=str(exc))


__all__ = ["TaskManager"]

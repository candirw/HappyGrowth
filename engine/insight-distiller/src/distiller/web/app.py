"""FastAPI 应用。

**架构判断：不引入 Node 前端工具链。**

用 Jinja2 服务端渲染 + 原生 JS，零构建步骤。理由：
1. 你明确要求易迁移到 Windows —— SPA 需要 Node + npm，多一整套依赖生态
2. 单用户本地工具，交互复杂度撑不起 SPA 的代价
3. 改完刷新就见效，迭代比「改代码 → 等构建」快

数据库连接按请求创建：SQLite 连接不是线程安全的，而 uvicorn 会用
线程池跑同步处理函数，共享连接会偶发 `ProgrammingError`。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..compat.paths import DataPaths, get_data_paths
from ..config import Config
from ..store import Database

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"


class WebDeps:
    """请求级依赖工厂。"""

    def __init__(self, config: Config, profile: str | None = None) -> None:
        self.config = config
        self.profile = profile
        self.paths: DataPaths = get_data_paths(
            config.storage.data_dir, use_system_dir=config.storage.use_system_dir
        )
        self.paths.ensure()

    @contextmanager
    def db(self) -> Iterator[Database]:
        """每请求一个连接。SQLite 连接不可跨线程共享。"""
        database = Database(self.paths.db)
        try:
            yield database
        finally:
            database.close()


def create_app(config: Config, *, profile: str | None = None):
    try:
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Web UI 需要额外依赖，请先安装：\n"
            "  pip install -e '.[web]'"
        ) from exc

    from .routes import api, pages

    deps = WebDeps(config, profile=profile)

    app = FastAPI(
        title="Insight Distiller",
        description="把视频/播客/博客/论文压缩成高密度结论",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.deps = deps

    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")

    return app


__all__ = ["WebDeps", "create_app"]

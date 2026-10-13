"""HTML 页面路由。只读——写操作全部走 JSON API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..views import digest_view, notes_view, relations_view, source_brief

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _deps(request: Request):
    return request.app.state.deps


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _page(request, "index.html", nav="library")


@router.get("/read/{source_id}", response_class=HTMLResponse)
def read(request: Request, source_id: int) -> HTMLResponse:
    """阅读页服务端渲染——首屏内容不该等 JS 请求完才出现。"""
    with _deps(request).db() as db:
        src = db.get_source(source_id)
        if src is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        digest = db.get_latest_digest(source_id)
        return _page(
            request,
            "read.html",
            nav="library",
            source=source_brief(src),
            digest=digest_view(src, digest) if digest else None,
            relations=relations_view(db, source_id),
            notes=notes_view(db, source_id),
        )


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request) -> HTMLResponse:
    return _page(request, "ask.html", nav="ask")


@router.get("/graph", response_class=HTMLResponse)
def graph_page(request: Request) -> HTMLResponse:
    return _page(request, "graph.html", nav="graph")


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    return _page(request, "review.html", nav="review")


@router.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request) -> HTMLResponse:
    return _page(request, "brief.html", nav="brief")


__all__ = ["router"]

"""命令行入口。

跨平台注意：用 rich 输出，它会自动处理 Windows 终端的颜色与编码问题；
--plain 开关可输出纯文本，兼容旧版 cmd 与管道重定向。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

# 必须在导入任何 HF 相关库之前设置，否则进度条仍会输出。
# 这些进度条会污染 CLI 表格与管道输出，Windows 旧终端下还可能乱码。
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .compat.caps import detect_caps
from .compat.fsio import write_text
from .config import ConfigError, load_config
from .ingest import FetchError, list_adapters
from .llm import build_provider
from .models import Digest, Source
from .pipeline import Pipeline, SubtitleMissing
from .render.markdown import render_digest

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Insight Distiller —— 把视频/播客/博客压缩成高密度结论",
)
console = Console()


def _err(msg: str) -> None:
    # 所有含外部数据的文本都必须 escape：rich 会把 [xxx] 当标签解析，
    # 导致 pip install -e ".[ocr]" 这类提示被吞掉一部分。
    console.print(f"[red]✗[/red] {escape(msg)}")


def _ok(msg: str) -> None:
    console.print(f"[green]✓[/green] {escape(msg)}")


def _load(config_path: Optional[str]):
    try:
        return load_config(config_path)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"insight-distiller {__version__}")


@app.command()
def add(
    target: str = typer.Argument(..., help="URL，或配合 --paste/--file 时的占位标题"),
    paste: bool = typer.Option(False, "--paste", help="从标准输入读取正文（反爬兜底）"),
    file: Optional[Path] = typer.Option(None, "--file", help="从本地文本文件导入"),
    force: bool = typer.Option(False, "--force", help="已消化过也重新跑"),
    subtitle_only: bool = typer.Option(
        False, "--subtitle-only",
        help="只要有字幕的：无字幕直接跳过，不启动本地转写",
    ),
    profile: Optional[str] = typer.Option(None, "--profile", help="local / hybrid / quality"),
    no_distill: bool = typer.Option(False, "--no-distill", help="只采集不浓缩"),
    config: Optional[str] = typer.Option(None, "--config", help="配置文件路径"),
) -> None:
    """消化一条内容：采集 → 浓缩 → 入库 → 导出 Markdown。"""
    cfg = _load(config)

    with Pipeline(cfg, profile=profile, progress=lambda m: console.print(f"  {escape(m)}")) as pipe:
        try:
            if paste:
                console.print("请粘贴正文，结束后按 Ctrl-D（Windows: Ctrl-Z 回车）：")
                text = sys.stdin.read()
                raw = pipe.from_paste(text, title=None if target == "-" else target)
            elif file:
                # 按后缀自动分流：丢 mp3 进来不该报「没有可用的采集器」，
                # 那个错误完全看不出真正原因
                if Pipeline.is_media_file(file):
                    raw = pipe.from_media(
                        str(file), title=None if target == "-" else target
                    )
                else:
                    raw = pipe.from_file(str(file))
            else:
                # 直接 `distill add ~/录音.m4a` 也该 work——
                # 要求用户记住加 --file 属于无谓的摩擦。
                #
                # 顺序很关键：论文判断必须在「本地文件」之前。
                # 本地 PDF 也是存在的文件，若先走文本读取会被当成
                # 纯文本解析失败，而它本该走论文分节链路。
                from pathlib import Path as _P

                from .paper.ingest import is_paper_url

                cand = _P(target).expanduser()
                if is_paper_url(target):
                    raw = pipe.fetch(target)
                elif Pipeline.is_media_file(cand) and cand.exists():
                    raw = pipe.from_media(str(cand))
                elif cand.exists() and cand.is_file() and "://" not in target:
                    raw = pipe.from_file(str(cand))
                else:
                    raw = pipe.fetch(target, subtitle_only=subtitle_only)
        except SubtitleMissing as exc:
            # 按预期跳过，不是错误——退出码用 0，方便脚本里批量调用
            console.print(f"[dim]{escape(str(exc))}[/dim]")
            raise typer.Exit(0) from exc
        except FetchError as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        except Exception as exc:  # noqa: BLE001
            # 论文链路会抛 PaperParseError 等自有异常类型。
            # 不兜住的话，用户丢个坏 PDF 就看到一整屏 traceback。
            _err(f"采集失败：{exc}")
            raise typer.Exit(1) from exc

        try:
            result = pipe.process(raw, force=force, distill=not no_distill)
        except Exception as exc:  # noqa: BLE001
            _err(f"浓缩失败：{exc}")
            raise typer.Exit(1) from exc

        src = result.source
        _ok(f"#{src.id} 「{src.title}」")
        if result.digest:
            d = result.digest
            if d.density_score is not None:
                console.print(f"  信息密度 [bold]{d.density_score}[/bold]/100 · {d.density_verdict}")
            if d.tldr:
                console.print("\n[bold]核心结论[/bold]")
                for item in d.tldr:
                    console.print(f"  · {escape(item)}")
            if d.contrarian:
                console.print("\n[bold yellow]反直觉观点[/bold yellow]")
                for item in d.contrarian:
                    console.print(f"  · {escape(item)}")
            cost = f"${d.cost_usd:.4f}" if d.cost_usd else "$0"
            console.print(
                f"\n  模型 {d.model_used} · tokens {d.tokens_in}/{d.tokens_out} · 成本 {cost}"
            )

        # 知识关联提示（M3.5）—— 这是「入库即连接」的用户可见部分
        if result.findings:
            console.print(f"\n[bold]⚡ 与你的知识库发现 {len(result.findings)} 处关联[/bold]")
            for line in result.findings:
                parts = line.splitlines()
                console.print(f"  ├─ {escape(parts[0])}")
                for sub in parts[1:]:
                    console.print(f"  │  {escape(sub.strip())}")

        meta_bits = []
        if result.domains:
            meta_bits.append(f"领域 {'、'.join(result.domains)}")
        if result.entities:
            meta_bits.append(f"{result.entities} 个概念")
        if result.claims:
            meta_bits.append(f"{result.claims} 条 claim")
        if meta_bits:
            console.print(f"\n  [dim]{escape(' · '.join(meta_bits))}[/dim]")
        if result.review_note:
            console.print(f"  [dim]{escape(result.review_note)}[/dim]")

        if result.md_path:
            console.print(f"  已导出 [dim]{result.md_path}[/dim]")
        console.print(f"\n完整浓缩版：[cyan]distill show {src.id}[/cyan]")
        if result.indexed_chunks:
            console.print(f"追问：[cyan]distill ask \"…\" --source {src.id}[/cyan]")
        if result.findings:
            console.print(f"看关系：[cyan]distill graph {src.id}[/cyan]")


@app.command(name="list")
def list_cmd(
    limit: int = typer.Option(20, "--limit", "-n"),
    content_type: Optional[str] = typer.Option(None, "--type", help="video/article/paper"),
    sort: str = typer.Option("created_at", "--sort", help="created_at / density_score"),
    min_score: Optional[float] = typer.Option(None, "--min-score", help="按信息密度过滤"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """列出已消化的内容。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        rows = pipe.db.list_sources(
            limit=limit, content_type=content_type,
            order_by=sort, min_density=min_score,
        )

    if not rows:
        console.print("知识库为空。用 [cyan]distill add <URL>[/cyan] 开始。")
        return

    table = Table(show_lines=False)
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("密度", justify="right")
    table.add_column("类型")
    table.add_column("标题", overflow="fold", max_width=52)
    table.add_column("来源")

    for s in rows:
        score = f"{s.density_score:.0f}" if s.density_score is not None else "-"
        style = "green" if (s.density_score or 0) >= 65 else (
            "yellow" if (s.density_score or 0) >= 40 else "dim"
        )
        table.add_row(
            str(s.id), f"[{style}]{score}[/{style}]",
            s.content_type.value, escape(s.title), s.platform,
        )
    console.print(table)


@app.command()
def show(
    source_id: int = typer.Argument(...),
    level: str = typer.Option("full", "--level", help="tldr / full"),
    export: Optional[Path] = typer.Option(None, "--export", help="导出 Markdown 到指定目录"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看浓缩结果。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        src = pipe.db.get_source(source_id)
        if src is None:
            _err(f"找不到 #{source_id}")
            raise typer.Exit(1)
        digest = pipe.db.get_latest_digest(source_id)
        if digest is None:
            _err(f"#{source_id} 尚未浓缩，可执行 distill add --force 重跑")
            raise typer.Exit(1)

    if level == "tldr":
        console.print(f"[bold]{escape(src.title)}[/bold]\n")
        for item in digest.tldr:
            console.print(f"· {escape(item)}")
        return

    md = render_digest(src, digest, frontmatter=False)
    from rich.markdown import Markdown

    console.print(Markdown(md))

    if export:
        from .compat.paths import safe_filename

        export.mkdir(parents=True, exist_ok=True)
        out = export / f"{safe_filename(f'{src.id:04d}-{src.title}')}.md"
        write_text(out, render_digest(src, digest, frontmatter=True))
        _ok(f"已导出 {out}")


@app.command()
def ask(
    question: str = typer.Argument(..., help="要问的问题"),
    source: Optional[int] = typer.Option(None, "--source", "-s", help="只问某一篇内容"),
    top: Optional[int] = typer.Option(None, "--top", help="送入模型的片段数"),
    show_context: bool = typer.Option(False, "--show-context", help="显示检索到的原文片段"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """基于知识库提问。回答强制带出处，无依据会明确说明。"""
    cfg = _load(config)
    with Pipeline(cfg, profile=profile) as pipe:
        from .search import ChatEngine

        engine = ChatEngine(
            cfg, pipe.db, profile=profile,
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
        )
        try:
            answer = engine.ask(
                question,
                source_ids=[source] if source else None,
                top_n=top,
            )
        except Exception as exc:  # noqa: BLE001
            _err(f"问答失败：{exc}")
            raise typer.Exit(1) from exc

    console.print()
    from rich.markdown import Markdown

    console.print(Markdown(answer.text))

    if answer.hits:
        console.print("\n[bold]引用来源[/bold]")
        for i, hit in enumerate(answer.hits, start=1):
            loc = f" @ {hit.anchor}" if hit.anchor else ""
            route = []
            if hit.vector_rank:
                route.append("语义")
            if hit.keyword_rank:
                route.append("关键词")
            tag = f"[dim]({'+'.join(route)})[/dim]" if route else ""
            console.print(
                f"  [{i}] #{hit.source_id} 《{escape(hit.title)}》{loc} {tag}"
            )

        if show_context:
            console.print("\n[bold]检索到的原文[/bold]")
            for i, hit in enumerate(answer.hits, start=1):
                console.print(f"\n[cyan][{i}][/cyan] {escape(hit.text[:400])}")

    if answer.cost_usd:
        console.print(f"\n[dim]{answer.model} · 成本 ${answer.cost_usd:.4f}[/dim]")


@app.command()
def index(
    source_id: Optional[int] = typer.Argument(None, help="指定内容；省略则补建全部缺失索引"),
    rebuild: bool = typer.Option(False, "--rebuild", help="重建（换 embedding 模型后需要）"),
    rebuild_fts: bool = typer.Option(
        False, "--rebuild-fts",
        help="仅重建关键词索引（不调用 embedding 模型，秒级完成）",
    ),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """建立或补建检索索引。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        # 关键词索引重建不需要 embedding 模型，单独走快路径
        if rebuild_fts:
            n = pipe.db.rebuild_fts()
            _ok(f"关键词索引已重建 · {n} 块")
            return

        from .search import Retriever

        if pipe.db.fts_needs_rebuild():
            console.print(
                "[yellow]![/yellow] 关键词索引待重建（中文分词规则已更新），"
                "正在自动重建…"
            )
            n = pipe.db.rebuild_fts()
            console.print(f"  [dim]关键词索引已重建 · {n} 块[/dim]")

        retriever = Retriever(
            pipe.db, cfg.retrieval,
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
        )
        model = cfg.retrieval.embedding_model

        if source_id:
            targets = [source_id]
        elif rebuild:
            targets = [s.id for s in pipe.db.list_sources(limit=10_000) if s.id]
        else:
            targets = pipe.db.sources_without_index(model)

        if not targets:
            console.print("所有内容都已建好索引。")
            return

        console.print(f"待处理 {len(targets)} 篇（模型 {model}）")
        total = 0
        for sid in targets:
            src = pipe.db.get_source(sid)
            if src is None:
                continue
            segments = pipe.db.get_segments(sid)
            if not segments:
                console.print(f"  [yellow]跳过[/yellow] #{sid}（无正文片段）")
                continue
            try:
                n = retriever.index_source(sid, segments)
            except Exception as exc:  # noqa: BLE001
                _err(f"#{sid} 建索引失败：{exc}")
                continue
            total += n
            console.print(f"  ✓ #{sid} {escape(src.title[:40])} · {n} 块")

        _ok(f"完成，共索引 {total} 块")


@app.command()
def search(
    query: str = typer.Argument(..., help="检索词"),
    limit: int = typer.Option(8, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """混合检索原文片段（不调用 LLM，零成本）。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        from .search import Retriever

        retriever = Retriever(
            pipe.db, cfg.retrieval,
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
        )
        hits = retriever.search(query, top_n=limit)

    if not hits:
        console.print("没有命中。可能尚未建索引，试试 [cyan]distill index[/cyan]。")
        return

    for i, hit in enumerate(hits, start=1):
        loc = f" @ {hit.anchor}" if hit.anchor else ""
        console.print(
            f"\n[cyan]{i}.[/cyan] #{hit.source_id} 《{escape(hit.title)}》{loc}"
        )
        console.print(f"   {escape(hit.text[:220])}")


def _render_candidates(plan) -> None:
    """候选表。展示足够信息让用户决定要不要跑、要剔掉哪些。"""
    table = Table(show_lines=False)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("标题", overflow="fold", max_width=46)
    table.add_column("时长", justify="right")
    table.add_column("播放", justify="right")
    table.add_column("收藏", justify="right")
    table.add_column("主题", justify="right")
    table.add_column("状态")

    for i, c in enumerate(plan.candidates, start=1):
        dur = f"{c.duration_sec // 60}分" if c.duration_sec else "-"
        topic = f"{c.topic_score:.2f}" if c.topic_score is not None else "-"
        status = "[dim]已在库[/dim]" if c.in_library else ""
        table.add_row(
            str(i), escape(c.title), dur,
            _fmt_num(c.play_count), _fmt_num(c.favorite_count),
            topic, status,
        )
    console.print(table)


def _fmt_num(n: int | None) -> str:
    if n is None:
        return "-"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _render_plan_header(plan) -> None:
    console.print("[bold]检索计划[/bold]")
    for line in plan.query.describe().splitlines():
        console.print(f"  {escape(line)}")

    if plan.query.assumptions:
        console.print("\n[bold]自动决定的部分[/bold] [dim](工具替你做的假设)[/dim]")
        for a in plan.query.assumptions:
            console.print(f"  · {escape(a)}")


def _render_estimate(est) -> None:
    console.print("\n[bold]成本预演[/bold]")
    console.print(
        f"  候选 {est.total} 条"
        + (f" · 已在库 {est.already_in_library} 条" if est.already_in_library else "")
        + f" · 待处理 {est.to_process} 条"
    )
    if est.need_asr:
        unknown = getattr(est, "subtitle_unknown", 0)
        if unknown == est.need_asr:
            # 全部未探测：说明是「上限估算」而非「确实都要转写」。
            # 说成「有字幕 0 条」会误导——多数 B 站视频有 CC 字幕，
            # 实际耗时与花费通常远低于此处预估。
            console.print(
                f"  [dim]字幕情况待确认 {unknown} 条 —— 以下为上限估算，"
                f"有字幕则跳过转写（快约 50 倍且免费）[/dim]"
            )
        else:
            detail = f"有字幕 {est.with_subtitle} 条"
            if unknown:
                detail += f" · 待确认 {unknown} 条"
            console.print(f"  需音频转写 {est.need_asr - unknown} 条（{detail}）")
    console.print(f"  预计耗时 [bold]{est.human_time()}[/bold] · 预估花费 [bold]${est.est_cost_usd:.4f}[/bold]")
    for w in est.warnings:
        console.print(f"  [yellow]![/yellow] {escape(w)}")


@app.command(name="plan")
def plan_cmd(
    instruction: str = typer.Argument(..., help="自然语言指令"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """只解析指令，看工具如何理解你的话（不发任何请求，零成本）。"""
    cfg = _load(config)
    from .discover import DiscoveryEngine

    engine = DiscoveryEngine(cfg, progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"))
    plan = engine.plan(instruction)

    _render_plan_header(plan)

    if plan.query.blocking_questions:
        console.print("\n[bold yellow]需要你澄清[/bold yellow] [dim](猜错代价较高，故先问)[/dim]")
        for q in plan.query.blocking_questions:
            console.print(f"  ? {escape(q.text)}")
            for i, opt in enumerate(q.options, start=1):
                console.print(f"      {i}) {escape(opt)}")
    else:
        console.print("\n[green]✓[/green] 指令已明确，可执行 [cyan]distill find \"…\"[/cyan]")


@app.command()
def find(
    instruction: str = typer.Argument(..., help="自然语言指令，如「某UP主摄影方向播放量前20」"),
    run: bool = typer.Option(False, "--run", help="确认后批量消化（默认只发现不消化）"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认，用于脚本化"),
    force: bool = typer.Option(False, "--force", help="已消化过也重新跑"),
    subtitle_only: bool = typer.Option(
        False, "--subtitle-only",
        help="只要有字幕的：无字幕直接跳过，不启动本地转写（批量导入必备）",
    ),
    no_distill: bool = typer.Option(
        False, "--no-distill", help="只采集不浓缩（无 LLM 时也能批量入库）"
    ),
    limit: Optional[int] = typer.Option(None, "--limit", help="覆盖数量"),
    exclude: Optional[str] = typer.Option(None, "--exclude", help="剔除序号，如 3,7"),
    pick: Optional[int] = typer.Option(None, "--pick", help="消歧时直接选第 N 个"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """用自然语言选材。默认只列候选，加 --run 才消化。"""
    cfg = _load(config)
    from .discover import DiscoveryError, DiscoveryEngine

    with Pipeline(cfg, profile=profile) as pipe:
        engine = DiscoveryEngine(
            cfg, pipe.db, profile=profile,
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
        )

        try:
            plan = engine.plan(instruction)
            if limit:
                plan.query.limit = limit

            _render_plan_header(plan)

            if plan.query.blocking_questions:
                console.print("\n[bold yellow]需要你澄清[/bold yellow]")
                for q in plan.query.blocking_questions:
                    console.print(f"  ? {escape(q.text)}")
                    for i, opt in enumerate(q.options, start=1):
                        console.print(f"      {i}) {escape(opt)}")
                console.print(
                    "\n请把答案补进指令后重试，例如："
                    "[cyan]distill find \"B站 影视飓风 摄影方向 前20\"[/cyan]"
                )
                raise typer.Exit(0)

            plan = engine.resolve(plan)

            if plan.resolve_options:
                if pick is None:
                    console.print(f"\n[bold yellow]「{escape(plan.query.targets[0].raw)}」匹配到多个账号[/bold yellow]")
                    for i, (_mid, name, desc) in enumerate(plan.resolve_options, start=1):
                        console.print(f"  {i}) {escape(name)} [dim]{escape(desc)}[/dim]")
                    console.print("\n用 [cyan]--pick N[/cyan] 指定，例如 --pick 1")
                    raise typer.Exit(0)
                plan = engine.pick_option(plan, pick)
                console.print(f"  已选择：{escape(plan.query.targets[0].resolved_name or '')}")

            plan = engine.discover(plan)

        except DiscoveryError as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc

        if plan.notes:
            console.print("\n[bold]检索过程[/bold]")
            for n in plan.notes:
                console.print(f"  · {escape(n)}")

        if not plan.candidates:
            console.print("\n没有符合条件的内容。可放宽主题或时间范围。")
            raise typer.Exit(0)

        console.print()
        _render_candidates(plan)

        if plan.estimate:
            _render_estimate(plan.estimate)

        # 剔除
        targets = list(plan.candidates)
        if exclude:
            drop = {int(x) for x in exclude.replace("，", ",").split(",") if x.strip().isdigit()}
            targets = [c for i, c in enumerate(targets, start=1) if i not in drop]
            console.print(f"\n已剔除 {len(drop)} 条，剩余 {len(targets)} 条")

        if not run:
            console.print(
                "\n[dim]以上仅为候选清单，未消化。加 --run 开始消化，"
                "可配合 --exclude 3,7 剔除。[/dim]"
            )
            raise typer.Exit(0)

        pending = targets if force else [c for c in targets if not c.in_library]
        if not pending:
            console.print(
                "\n全部内容已在库中，无需重复消化。"
                "[dim] 需要重新浓缩可加 --force。[/dim]"
            )
            raise typer.Exit(0)

        if not yes:
            confirm = typer.confirm(f"\n将消化 {len(pending)} 条内容，继续？", default=True)
            if not confirm:
                console.print("已取消。")
                raise typer.Exit(0)

        # 批量消化
        #
        # 这段的形态来自实测教训：抓 211 期系列时，原实现让人无法判断
        # 「还要多久」「卡住了没」「中断了怎么续」，我当时只能自己写外部脚本
        # 配 nohup + tail 日志才跑完。批量导入是本工具的核心场景之一，
        # 不该退化到需要用户绕过 CLI。
        ok = failed = skipped = 0
        failures: list[tuple[str, str]] = []
        started = time.monotonic()
        interrupted = False

        for i, cand in enumerate(pending, start=1):
            # ETA 单独一行：标题本就可能占满宽度，拼在一起会折行，
            # 反而看不清进度。前 3 条不估算——样本太少给出的数字会误导。
            eta = ""
            if i > 3:
                per = (time.monotonic() - started) / (i - 1)
                eta = f"  [dim]剩余约 {_fmt_duration(per * (len(pending) - i + 1))}[/dim]"
            console.print(
                f"\n[cyan][{i}/{len(pending)}][/cyan]{eta}"
            )
            console.print(f"  {escape(cand.title[:70])}")
            try:
                raw = pipe.fetch(cand.url, subtitle_only=subtitle_only)
                result = pipe.process(raw, force=force, distill=not no_distill)
                d = result.digest
                if d and d.density_score is not None:
                    console.print(f"  密度 {d.density_score} · {d.density_verdict}")
                ok += 1
            except SubtitleMissing as exc:
                # 按预期跳过，不算失败——混进失败数会让人以为出了问题
                console.print(f"  [dim]跳过：{escape(str(exc)[:100])}[/dim]")
                skipped += 1
            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception as exc:  # noqa: BLE001 - 单条失败继续处理其余
                console.print(f"  [yellow]失败[/yellow]：{escape(str(exc)[:110])}")
                failures.append((cand.url, str(exc)[:120]))
                failed += 1

        console.print()
        elapsed = _fmt_duration(time.monotonic() - started)
        if interrupted:
            console.print(
                f"[yellow]已中断[/yellow]（用时 {elapsed}）："
                f"成功 {ok} 条，剩余 {len(pending) - i + 1} 条未处理。"
            )
            # 已完成的都已入库，重跑同一条指令会自动跳过，因此续跑是安全的
            console.print(
                "[dim]已完成的内容已入库。重跑同一条指令即可继续，"
                "已消化的会自动跳过。[/dim]"
            )
        else:
            summary = f"完成：成功 {ok} 条"
            if skipped:
                summary += f"，跳过 {skipped} 条（无字幕）"
            if failed:
                summary += f"，失败 {failed} 条"
            summary += f" · 用时 {elapsed}"
            _ok(summary)

        if failures:
            console.print("\n[bold]失败清单[/bold]")
            for url, reason in failures[:10]:
                console.print(f"  [dim]{escape(url)}[/dim]")
                console.print(f"    {escape(reason)}")
            if len(failures) > 10:
                console.print(f"  [dim]… 另有 {len(failures) - 10} 条[/dim]")
            console.print(
                "\n[dim]重跑同一条指令会自动跳过已成功的，只重试这些。[/dim]"
            )

        console.print(f"查看：[cyan]distill list --sort density_score[/cyan]")


def _fmt_duration(seconds: float) -> str:
    """人类可读的时长。批量任务动辄几十分钟，秒数不可读。"""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total // 3600} 小时 {(total % 3600) // 60} 分"


paper_app = typer.Typer(no_args_is_help=True, help="论文相关：相似检索、阅读路径、横向对比")
app.add_typer(paper_app, name="paper")


@paper_app.command("similar")
def paper_similar(
    target: str = typer.Argument(..., help="库内编号、arXiv ID/链接、DOI 或标题"),
    per_group: Optional[int] = typer.Option(None, "--per-group", "-n", help="每组展示数量"),
    strategy: Optional[str] = typer.Option(
        None, "--strategy", help="限定召回路：citation / coupling / semantic，逗号分隔"
    ),
    unread: bool = typer.Option(False, "--unread", help="只看未消化过的"),
    add: Optional[str] = typer.Option(None, "--add", help="直接消化第 N 篇，如 1,3"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """找相似论文。四路召回后分组：奠基工作 / 同期竞品 / 后续进展 / 跨领域启发。"""
    cfg = _load(config)
    from .paper import SimilarFinder, parse_ref

    with Pipeline(cfg, progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]")) as pipe:
        ref, meta = _resolve_paper_target(pipe, target)

        console.print(f"\n[bold]{escape(meta.title or target)}[/bold]")
        info = []
        if meta.year:
            info.append(str(meta.year))
        if meta.citation_count is not None:
            info.append(f"被引 {meta.citation_count}")
        if meta.venue:
            info.append(meta.venue)
        info.append("预印本" if meta.is_preprint else "已发表")
        console.print(f"[dim]{escape(' · '.join(info))}[/dim]\n")

        strategies = None
        if strategy:
            strategies = {s.strip() for s in strategy.replace("，", ",").split(",") if s.strip()}

        finder = SimilarFinder(
            pipe.academic_client(),
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
            db=pipe.db,
        )
        result = finder.find(
            ref, meta,
            limit_per_group=per_group or cfg.paper.similar_per_group,
            strategies=strategies,
            unread_only=unread,
        )

        if not result.total:
            _err("没有找到相似论文。")
            if result.degraded:
                console.print("[yellow]数据源状态[/yellow]")
                for d in result.degraded:
                    console.print(f"  · {escape(d)}")
            raise typer.Exit(1)

        console.print()
        index = 0
        flat: list = []
        for name, hint, papers in result.ordered():
            console.print(f"[bold cyan]【{name}】[/bold cyan] [dim]{escape(hint)}[/dim]")
            for p in papers:
                index += 1
                flat.append(p)
                mark = f" [green]✓已在库 #{p.library_id}[/green]" if p.in_library else ""
                bits = [p.reason]
                if p.ref.year:
                    bits.insert(0, str(p.ref.year))
                console.print(f"  {index:>2}. {escape(p.title[:62])}")
                console.print(f"      [dim]{escape(' · '.join(bits))}[/dim]{mark}")
            console.print()

        if result.notes:
            console.print("[dim]检索过程[/dim]")
            for n in result.notes:
                console.print(f"[dim]  · {escape(n)}[/dim]")
        if result.degraded:
            console.print("\n[yellow]部分召回路不可用[/yellow] [dim](已降级，结果可能不完整)[/dim]")
            for d in result.degraded:
                console.print(f"  [yellow]·[/yellow] {escape(d)}")

        if add:
            picks = [
                int(x) for x in add.replace("，", ",").split(",")
                if x.strip().isdigit()
            ]
            chosen = [flat[i - 1] for i in picks if 1 <= i <= len(flat)]
            if not chosen:
                _err("--add 的序号无效")
                raise typer.Exit(1)
            console.print()
            for p in chosen:
                if p.in_library:
                    console.print(f"[dim]跳过（已在库）：{escape(p.title[:50])}[/dim]")
                    continue
                url = p.ref.url
                if not url:
                    console.print(f"[yellow]跳过（无可用链接）：{escape(p.title[:50])}[/yellow]")
                    continue
                console.print(f"\n消化：{escape(p.title[:56])}")
                try:
                    raw = pipe.fetch(url)
                    pipe.process(raw)
                    _ok("完成")
                except Exception as exc:  # noqa: BLE001
                    console.print(f"  [yellow]失败[/yellow]：{escape(str(exc)[:110])}")
        else:
            console.print(
                f"\n[dim]用 --add 1,3 直接消化指定论文；--unread 只看未读[/dim]"
            )


@paper_app.command("lineage")
def paper_lineage(
    target: str = typer.Argument(..., help="库内编号、arXiv ID/链接、DOI 或标题"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """构建阅读路径：按引文依赖告诉你该先读哪几篇。"""
    cfg = _load(config)
    from .paper import build_lineage

    with Pipeline(cfg, progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]")) as pipe:
        ref, meta = _resolve_paper_target(pipe, target)
        lineage = build_lineage(
            pipe.academic_client(), ref, meta,
            limit=limit or cfg.paper.lineage_limit,
            db=pipe.db,
            progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
        )

        console.print(f"\n[bold]目标论文[/bold]：{escape(lineage.target[:66])}")
        if not lineage.steps:
            console.print("\n没有找到明确的必读前置。")
            for n in lineage.notes + lineage.degraded:
                console.print(f"  [dim]· {escape(n)}[/dim]")
            raise typer.Exit(0)

        console.print("\n[bold]建议阅读顺序[/bold] [dim](先读基础，再回到目标论文)[/dim]\n")
        for step in lineage.steps:
            mark = f" [green]✓已在库 #{step.library_id}[/green]" if step.in_library else ""
            year = f"{step.year}" if step.year else "----"
            console.print(f"  {step.order}. {escape(step.title[:60])}{mark}")
            console.print(
                f"     [dim]{year} · 被引 {step.citations} · {escape(step.reason)}[/dim]"
            )
        console.print(f"\n  ▸ 最后读：[bold]{escape(lineage.target[:56])}[/bold]")

        for n in lineage.notes:
            console.print(f"\n[dim]{escape(n)}[/dim]")
        for d in lineage.degraded:
            console.print(f"[yellow]·[/yellow] {escape(d)}")


@paper_app.command("compare")
def paper_compare(
    ids: list[int] = typer.Argument(..., help="库内论文编号，至少 2 个"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """横向对比多篇已消化的论文，生成方法/数据集/指标对比。"""
    cfg = _load(config)
    if len(ids) < 2:
        _err("至少需要 2 篇论文")
        raise typer.Exit(1)

    from .llm import LLMRouter
    from .paper import compare_papers

    with Pipeline(cfg) as pipe:
        papers: list[tuple[str, dict]] = []
        for sid in ids:
            src = pipe.db.get_source(sid)
            if src is None:
                _err(f"找不到 #{sid}")
                raise typer.Exit(1)
            digest = pipe.db.get_latest_digest(sid)
            if digest is None:
                _err(f"#{sid} 尚未浓缩")
                raise typer.Exit(1)
            # 从 Digest 还原对比所需字段
            payload = {
                "problem": digest.tldr[0] if digest.tldr else "",
                "method": {
                    "core_idea": next(
                        (t for t in digest.tldr if t.startswith("方法：")), ""
                    ),
                    "detail": [
                        p for item in digest.outline
                        if item.title == "方法细节" for p in item.points
                    ],
                },
                "experiments": {
                    "metrics": [
                        {"name": p.split("：")[0], "value": p.split("：", 1)[-1]}
                        for item in digest.outline
                        if item.title == "实验与结果" for p in item.points
                        if "：" in p
                    ],
                },
                "conclusion": digest.tldr,
                "limitations": digest.open_questions,
            }
            papers.append((src.title, payload))

        router = LLMRouter(cfg)
        try:
            data = compare_papers(
                router, papers,
                progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
            )
        except Exception as exc:  # noqa: BLE001
            _err(f"对比失败：{exc}")
            raise typer.Exit(1) from exc

    rows = data.get("rows") or []
    dims = data.get("dimensions") or []
    if rows and dims:
        table = Table(show_lines=True)
        table.add_column("论文", overflow="fold", max_width=26)
        for d in dims[:4]:
            table.add_column(escape(str(d)), overflow="fold", max_width=30)
        for row in rows:
            values = row.get("values") or {}
            table.add_row(
                escape(str(row.get("paper", ""))[:40]),
                *[escape(str(values.get(d, "未提及"))[:120]) for d in dims[:4]],
            )
        console.print(table)

    for key, title in (
        ("key_differences", "关键差异"),
        ("contradictions", "结论矛盾"),
    ):
        items = data.get(key) or []
        if items:
            style = "yellow" if key == "contradictions" else "bold"
            console.print(f"\n[{style}]{title}[/{style}]")
            for x in items:
                console.print(f"  · {escape(str(x))}")

    if data.get("recommendation"):
        console.print(f"\n[bold]选择建议[/bold]\n  {escape(str(data['recommendation']))}")
    if data.get("cost_usd"):
        console.print(f"\n[dim]{data.get('model_used')} · ${data['cost_usd']:.4f}[/dim]")


def _resolve_paper_target(pipe, target: str):
    """把用户输入解析为 (PaperRef, PaperMeta)。

    支持库内编号——用户更习惯用 `distill list` 看到的编号。
    """
    from .paper import parse_ref

    if target.isdigit():
        src = pipe.db.get_source(int(target))
        if src is None:
            _err(f"找不到 #{target}")
            raise typer.Exit(1)
        extra = {}
        row = pipe.db.conn.execute(
            "SELECT extra_json FROM sources WHERE id = ?", (int(target),)
        ).fetchone()
        if row:
            import json

            extra = json.loads(row["extra_json"] or "{}")
        ref = parse_ref(extra.get("arxiv_id") or extra.get("doi") or src.url)
        if ref.is_empty:
            ref = parse_ref(src.title)
    else:
        ref = parse_ref(target)

    if ref.is_empty:
        _err(f"无法识别论文标识：{target}")
        raise typer.Exit(1)

    meta = pipe.academic_client().resolve_meta(ref)
    if not meta.title:
        _err(
            "未能获取论文元数据。学术 API 可能限流，请稍后重试"
            + (f"（{'; '.join(pipe.academic_client().degraded)}）"
               if pipe.academic_client().degraded else "")
        )
        raise typer.Exit(1)
    return ref, meta


@app.command()
def graph(
    source_id: Optional[int] = typer.Argument(None, help="以某篇为中心；省略则看全库概览"),
    depth: int = typer.Option(2, "--depth", "-d", help="遍历跳数"),
    limit: int = typer.Option(25, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看知识图谱：内容之间是什么关系。"""
    cfg = _load(config)
    from .knowledge import REL_LABELS, build_graph

    with Pipeline(cfg) as pipe:
        if source_id is None:
            rows = pipe.db.conn.execute(
                "SELECT rel_type, COUNT(*) AS n FROM relations "
                "WHERE verified_by_user IS NULL OR verified_by_user = 1 "
                "GROUP BY rel_type ORDER BY n DESC"
            ).fetchall()
            total = sum(int(r["n"]) for r in rows)
            if not total:
                console.print(
                    "知识库还没有建立关联。消化 2 篇以上相关内容后会自动关联，"
                    "或执行 [cyan]distill link --all[/cyan] 补建。"
                )
                raise typer.Exit(0)

            console.print(f"[bold]全库关系概览[/bold]（共 {total} 条）\n")
            for r in rows:
                icon, label = REL_LABELS.get(r["rel_type"], ("·", r["rel_type"]))
                console.print(f"  {icon} {label:12s} {r['n']:>4} 条")

            hot = pipe.db.conn.execute(
                "SELECT s.id, s.title, COUNT(*) AS n FROM relations r "
                "JOIN sources s ON s.id IN (r.from_source, r.to_source) "
                "GROUP BY s.id ORDER BY n DESC LIMIT 5"
            ).fetchall()
            if hot:
                console.print("\n[bold]关联最多的内容[/bold]")
                for r in hot:
                    console.print(f"  #{r['id']} {escape(r['title'][:46])} [dim]({r['n']} 条关系)[/dim]")
            raise typer.Exit(0)

        try:
            view = build_graph(pipe.db, source_id, depth=depth, limit=limit)
        except ValueError as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc

        console.print(f"[bold]{escape(view.center_title[:66])}[/bold] [dim]#{view.center_id}[/dim]\n")

        if view.relations:
            console.print("[bold]直接关系[/bold]")
            for rel in view.relations:
                arrow = "→" if rel["direction"] == "out" else "←"
                console.print(
                    f"  {rel['icon']} {rel['label']} {arrow} #{rel['id']} "
                    f"{escape(rel['title'][:42])} [dim]({rel['confidence']:.2f})[/dim]"
                )

        multi_hop = [n for n in view.nodes if n.distance > 1]
        if multi_hop:
            console.print(f"\n[bold]间接关联[/bold] [dim]({depth} 跳内)[/dim]")
            for node in multi_hop:
                console.print(
                    f"  {'·' * node.distance} #{node.id} {escape(node.title[:44])} "
                    f"[dim]{node.distance} 跳[/dim]"
                )

        if not view.relations and not multi_hop:
            console.print("[dim]该内容暂无关联。可执行 distill link 3 重新分析。[/dim]")


@app.command()
def gaps(
    limit: int = typer.Option(15, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """知识盲区：被反复提及但你从未深入的概念。"""
    cfg = _load(config)
    from .knowledge import find_gaps

    with Pipeline(cfg) as pipe:
        items = find_gaps(pipe.db, limit=limit)

    if not items:
        console.print("暂未发现明显的知识盲区（需要先消化若干内容并抽取实体）。")
        return

    console.print("[bold]知识盲区[/bold] [dim](高频出现但覆盖不足的概念)[/dim]\n")
    table = Table()
    table.add_column("概念", overflow="fold", max_width=28)
    table.add_column("提及", justify="right")
    table.add_column("说明", overflow="fold", max_width=46)
    for gap in items:
        table.add_row(escape(gap.name), str(gap.mention_count), escape(gap.detail))
    console.print(table)
    console.print(
        "\n[dim]建议：用 distill find \"<概念>\" 找相关内容补齐，"
        "或 distill ask \"<概念>是什么\" 看现有资料怎么说[/dim]"
    )


@app.command()
def entity(
    name: Optional[str] = typer.Argument(None, help="概念名；省略则列出高频实体"),
    limit: int = typer.Option(25, "--limit", "-n"),
    merge: Optional[str] = typer.Option(
        None, "--merge", help="合并实体：--merge \"别名\" 把它并入指定概念"
    ),
    prune: bool = typer.Option(False, "--prune", help="清理只出现一次的实体"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看概念在库中的分布，或维护实体表。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        if prune:
            n = pipe.db.prune_entities(min_mentions=cfg.knowledge.entity_min_mentions)
            _ok(f"已清理 {n} 个低频实体")
            raise typer.Exit(0)

        if name and merge:
            keep = pipe.db.find_entity(name)
            drop = pipe.db.find_entity(merge)
            if keep is None or drop is None:
                _err(f"找不到实体：{name if keep is None else merge}")
                raise typer.Exit(1)
            pipe.db.merge_entities(int(keep["id"]), int(drop["id"]))
            _ok(f"已把「{merge}」合并进「{name}」")
            raise typer.Exit(0)

        if not name:
            rows = pipe.db.list_entities(
                min_mentions=cfg.knowledge.entity_min_mentions, limit=limit
            )
            if not rows:
                console.print(
                    "实体表为空。消化内容后会自动抽取，"
                    "或执行 [cyan]distill link --all[/cyan] 补建。"
                )
                raise typer.Exit(0)
            table = Table()
            table.add_column("概念", overflow="fold", max_width=28)
            table.add_column("类型")
            table.add_column("覆盖内容", justify="right")
            table.add_column("定义", overflow="fold", max_width=42)
            for r in rows:
                table.add_row(
                    escape(r["name"]), r["type"] or "-",
                    str(r["mention_count"]),
                    escape((r["definition"] or "")[:80]),
                )
            console.print(table)
            raise typer.Exit(0)

        row = pipe.db.find_entity(name)
        if row is None:
            _err(f"库中没有概念「{name}」")
            raise typer.Exit(1)

        import json as _json

        aliases = _json.loads(row["aliases_json"] or "[]")
        console.print(f"[bold]{escape(row['name'])}[/bold] [dim]{row['type'] or ''}[/dim]")
        if aliases:
            console.print(f"别名：{escape('、'.join(aliases))}")
        if row["definition"]:
            console.print(f"定义：{escape(row['definition'])}")
        console.print(f"\n在 [bold]{row['mention_count']}[/bold] 篇内容中出现：\n")
        for s in pipe.db.entity_sources(int(row["id"])):
            console.print(f"  #{s['id']} [{s['content_type']}] {escape(s['title'][:52])}")


@app.command()
def timeline(
    topic: str = typer.Argument(..., help="主题词"),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """某主题的认知演进时间线。"""
    cfg = _load(config)
    from .knowledge import build_timeline

    with Pipeline(cfg) as pipe:
        entries = build_timeline(pipe.db, topic, limit=limit)

    if not entries:
        console.print(f"库中没有关于「{topic}」的内容。")
        return

    console.print(f"[bold]「{escape(topic)}」的认知演进[/bold]\n")
    for e in entries:
        when = e.date or "时间未知"
        kind = f"[{e.claim_type}]" if e.claim_type else ""
        console.print(f"  {when}  #{e.source_id} {escape(e.title[:40])} [dim]{kind}[/dim]")
        if e.predicate:
            console.print(f"            [dim]{escape(e.predicate[:72])}[/dim]")


@app.command()
def link(
    source_id: Optional[int] = typer.Argument(None, help="指定内容；省略需配 --all"),
    all_sources: bool = typer.Option(False, "--all", help="为全部已消化内容补建关联"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """补建知识关联（实体、claim、关系）。用于历史内容或调整配置后重跑。"""
    cfg = _load(config)
    if source_id is None and not all_sources:
        _err("请指定内容编号，或用 --all 处理全部")
        raise typer.Exit(1)

    with Pipeline(cfg, profile=profile, progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]")) as pipe:
        if all_sources:
            targets = [s.id for s in pipe.db.list_sources(limit=10_000) if s.id]
        else:
            targets = [source_id]

        from .llm import LLMRouter

        done = 0
        for sid in targets:
            src = pipe.db.get_source(sid)
            digest = pipe.db.get_latest_digest(sid) if src else None
            if src is None or digest is None:
                continue
            segments = pipe.db.get_segments(sid)
            if not segments:
                continue

            console.print(f"\n#{sid} {escape(src.title[:50])}")
            raw = pipe.raw_from_db(src, segments)
            out = pipe._try_link(sid, raw, digest, LLMRouter(cfg, profile))
            bits = []
            if out["entities"]:
                bits.append(f"{out['entities']} 个实体")
            if out["claims"]:
                bits.append(f"{out['claims']} 条 claim")
            if out["domains"]:
                bits.append(f"领域 {'、'.join(out['domains'])}")
            console.print(f"  {escape('、'.join(bits) or '无新增')}")
            for line in out["findings"]:
                for sub in line.splitlines():
                    console.print(f"  {escape(sub)}")
            done += 1

        console.print()
        _ok(f"已处理 {done} 篇")


@app.command()
def claims(
    subject: Optional[str] = typer.Argument(None, help="主题关键词"),
    domain: Optional[str] = typer.Option(None, "--domain", help="领域，如 finance"),
    claim_type: Optional[str] = typer.Option(
        None, "--type", help="fact / data / opinion / prediction / method"
    ),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看原子知识单元。区分事实/观点/预测，供 agent 消费（M6）。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        rows = pipe.db.search_claims(
            domain=domain,
            claim_types=[claim_type] if claim_type else None,
            subject=subject,
            limit=limit,
        )

    if not rows:
        total = 0
        with Pipeline(cfg) as pipe:
            total = pipe.db.count_claims()
        if total == 0:
            console.print(
                "还没有抽取 claim。消化高密度内容时会自动抽取，"
                "或执行 [cyan]distill link --all[/cyan] 补建。"
            )
        else:
            console.print(f"没有匹配的 claim（库中共 {total} 条）。")
        return

    _TYPE_STYLE = {
        "fact": "green", "data": "cyan", "opinion": "yellow",
        "prediction": "magenta", "method": "blue",
    }
    _TYPE_LABEL = {
        "fact": "事实", "data": "数据", "opinion": "观点",
        "prediction": "预测", "method": "方法",
    }

    import json as _json

    for r in rows:
        style = _TYPE_STYLE.get(r["claim_type"], "white")
        label = _TYPE_LABEL.get(r["claim_type"], r["claim_type"])
        when = f" · {r['as_of']}" if r["as_of"] else ""
        console.print(
            f"\n[{style}][{label}][/{style}]{when} "
            f"[dim]#{r['source_id']} {escape((r['title'] or '')[:34])}[/dim]"
        )
        console.print(f"  {escape(r['predicate'])}")
        numeric = _json.loads(r["numeric_json"] or "{}")
        if numeric:
            console.print(f"  [dim]数值：{escape(_json.dumps(numeric, ensure_ascii=False))}[/dim]")

    console.print(
        "\n[dim]提示：agent 应只用 fact/data 做计算，"
        "opinion/prediction 需标注为「某人观点」[/dim]"
    )


@app.command()
def review(
    grade: Optional[str] = typer.Option(
        None, "--grade", "-g", help="直接给上一条打分：forgot / ok / easy"
    ),
    source_id: Optional[int] = typer.Option(
        None, "--id", help="配合 --grade 指定内容编号"
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    upcoming: bool = typer.Option(False, "--upcoming", help="连未到期的一起看"),
    star: Optional[int] = typer.Option(None, "--star", help="标记必看"),
    unstar: Optional[int] = typer.Option(None, "--unstar", help="取消必看"),
    drop: Optional[int] = typer.Option(None, "--drop", help="从重温队列移除"),
    rebuild: bool = typer.Option(False, "--rebuild", help="为历史内容批量判定入队"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """该回头看的内容。只调度有可执行项/疑问/关联的高密度内容。"""
    cfg = _load(config)
    from .review import GRADE_LABELS, GRADES, ReviewScheduler

    with Pipeline(cfg) as pipe:
        sched = ReviewScheduler(
            pipe.db,
            min_density=cfg.review.min_density,
            require_value=cfg.review.require_value,
        )

        if rebuild:
            added, skipped = sched.auto_enqueue_all()
            _ok(f"新增 {added} 篇入队，跳过 {skipped} 篇（价值不足）")
            raise typer.Exit(0)

        for sid, flag in ((star, True), (unstar, False)):
            if sid is not None:
                src = pipe.db.get_source(sid)
                if src is None:
                    _err(f"找不到 #{sid}")
                    raise typer.Exit(1)
                sched.set_starred(sid, flag)
                _ok(f"{'已标记必看' if flag else '已取消必看'}：{src.title[:40]}")
                raise typer.Exit(0)

        if drop is not None:
            sched.remove(drop)
            _ok(f"已从重温队列移除 #{drop}")
            raise typer.Exit(0)

        if grade:
            if grade not in GRADES:
                _err(f"无效反馈「{grade}」，可选：{' / '.join(GRADES)}")
                raise typer.Exit(1)
            if source_id is None:
                _err("请用 --id 指定内容编号")
                raise typer.Exit(1)
            interval, due = sched.grade(source_id, grade)
            _ok(f"已记录「{GRADE_LABELS[grade]}」· 下次 {due}（{interval} 天后）")
            raise typer.Exit(0)

        stats = sched.stats()
        items = sched.due_items(
            limit=limit or cfg.review.daily_limit, include_future=upcoming
        )

        if not items:
            if stats["total"] == 0:
                console.print(
                    "重温队列为空。消化内容时会自动判定，"
                    "或执行 [cyan]distill review --rebuild[/cyan] 为历史内容补建。"
                )
            else:
                console.print(
                    f"[green]✓[/green] 今天没有要重温的内容"
                    f"（队列共 {stats['total']} 篇，均未到期）。\n"
                    f"[dim]用 --upcoming 查看后续安排[/dim]"
                )
            raise typer.Exit(0)

        console.print(
            f"[bold]待重温 {len(items)} 篇[/bold] "
            f"[dim](队列 {stats['total']} · 到期 {stats['due']} · 必看 {stats['starred']})[/dim]\n"
        )

        for item in items:
            star_mark = " ⭐" if item.starred else ""
            overdue = f" [yellow]逾期 {item.overdue_days} 天[/yellow]" if item.overdue_days else ""
            density = f" · 密度 {item.density:.0f}" if item.density is not None else ""
            console.print(
                f"[bold cyan]#{item.source_id}[/bold cyan] "
                f"{escape(item.title[:56])}{star_mark}{overdue}"
            )
            console.print(
                f"  [dim]{item.content_type}{density} · "
                f"第 {item.review_count + 1} 次 · 间隔 {item.interval_days} 天[/dim]"
            )
            if item.reasons:
                console.print(f"  [dim]为什么推给你：{escape('；'.join(item.reasons))}[/dim]")

            if item.tldr:
                console.print()
                for t in item.tldr:
                    console.print(f"    · {escape(t)}")

            if item.actions:
                console.print(f"\n  [bold]待办[/bold]")
                for a in item.actions:
                    console.print(f"    [ ] {escape(a)}")

            if item.open_questions:
                console.print(f"\n  [bold]存疑[/bold]")
                for q in item.open_questions:
                    console.print(f"    ? {escape(q)}")

            if item.notes:
                console.print(f"\n  [bold]我的批注[/bold]")
                for n in item.notes:
                    console.print(f"    · {escape(n)}")

            console.print()

        console.print(
            "[dim]打分：[/dim]"
            f"[cyan]distill review -g ok --id {items[0].source_id}[/cyan]"
            "  [dim]（forgot / ok / easy）[/dim]"
        )
        console.print(
            f"[dim]记批注：[/dim][cyan]distill note {items[0].source_id} \"想法\"[/cyan]"
        )


@app.command()
def note(
    source_id: int = typer.Argument(..., help="内容编号"),
    text: Optional[str] = typer.Argument(None, help="批注内容"),
    note_type: str = typer.Option(
        "comment", "--type", "-t",
        help="comment 想法 / todo 待办 / disagree 不同意 / insight 洞察 / done 已完成",
    ),
    list_only: bool = typer.Option(False, "--list", "-l", help="只看已有批注"),
    delete: Optional[int] = typer.Option(None, "--delete", help="删除指定批注 id"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """记录自己的批注。这是你的思考，比 AI 摘要更值钱。"""
    cfg = _load(config)
    _TYPES = ("comment", "todo", "disagree", "insight", "done")

    with Pipeline(cfg) as pipe:
        src = pipe.db.get_source(source_id)
        if src is None:
            _err(f"找不到 #{source_id}")
            raise typer.Exit(1)

        if delete is not None:
            if pipe.db.delete_note(delete):
                _ok(f"已删除批注 #{delete}")
            else:
                _err(f"找不到批注 #{delete}")
            raise typer.Exit(0)

        if list_only or not text:
            notes = pipe.db.get_notes(source_id)
            console.print(f"[bold]{escape(src.title[:60])}[/bold]\n")
            if not notes:
                console.print("还没有批注。")
                console.print(
                    f"\n[dim]添加：[/dim][cyan]distill note {source_id} \"你的想法\"[/cyan]"
                )
                raise typer.Exit(0)
            for n in notes:
                # 转义类型标签，否则 rich 会把 [todo] 当 markup 吃掉
                console.print(
                    f"  {escape('[' + n['note_type'] + ']')} #{n['id']} {escape(n['text'])}"
                )
                console.print(f"      [dim]{n['created_at'][:16]}[/dim]")
            raise typer.Exit(0)

        if note_type not in _TYPES:
            _err(f"无效类型「{note_type}」，可选：{' / '.join(_TYPES)}")
            raise typer.Exit(1)

        nid = pipe.db.add_note(source_id, text, note_type=note_type)
        _ok(f"已记录批注 #{nid} 到《{src.title[:36]}》")


@app.command()
def brief(
    days: Optional[int] = typer.Option(None, "--days", "-d", help="统计区间"),
    export: Optional[str] = typer.Option(None, "--export", help="导出 Markdown 到指定路径"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """周期简报：新关联、悬着的待办、疑问闭环、知识盲区。零 LLM 成本。"""
    cfg = _load(config)
    from .review import build_brief, render_brief_markdown

    with Pipeline(cfg) as pipe:
        b = build_brief(pipe.db, days=days or cfg.review.brief_days)

    if b.is_empty:
        console.print(
            f"近 {b.period_days} 天没有新增内容，知识库也没有待处理项。"
        )
        return

    if export:
        from .compat.fsio import write_text

        path = Path(export).expanduser()
        if path.is_dir():
            path = path / f"简报-{b.since}.md"
        write_text(path, render_brief_markdown(b))
        _ok(f"已导出 {path}")
        return

    console.print(
        f"[bold]知识简报[/bold] [dim]· 近 {b.period_days} 天（自 {b.since}）[/dim]\n"
    )

    type_bits = " · ".join(f"{k} {v}" for k, v in sorted(b.by_type.items()))
    console.print(
        f"新增 [bold]{b.new_count}[/bold] 篇"
        + (f"（{type_bits}）" if type_bits else "")
        + f" · 约 {b.total_chars // 1000}k 字"
    )
    if b.due_reviews:
        console.print(f"待重温 [bold yellow]{b.due_reviews}[/bold yellow] 篇 [dim](distill review)[/dim]")

    # 冲突最值得先看——认知升级多来自这里
    if b.contradictions:
        console.print(f"\n[bold yellow]⚠️  需要你判断的结论冲突[/bold yellow]")
        for c in b.contradictions:
            console.print(
                f"  #{c['from_id']}《{escape(c['from_title'][:30])}》 ↔ "
                f"#{c['to_id']}《{escape(c['to_title'][:30])}》"
            )
            if c.get("new_claim"):
                console.print(f"     前者：{escape(str(c['new_claim'])[:62])}")
                console.print(f"     后者：{escape(str(c['old_claim'])[:62])}")
            elif c.get("note"):
                console.print(f"     [dim]{escape(str(c['note'])[:62])}[/dim]")

    if b.pending_actions:
        console.print(f"\n[bold]📌 悬着的可执行项[/bold]")
        for a in b.pending_actions:
            age = f" [dim]({a.age_days} 天前)[/dim]" if a.age_days else ""
            console.print(f"  [ ] {escape(a.text[:66])}{age}")
            console.print(f"      [dim]#{a.source_id}《{escape(a.title[:34])}》[/dim]")

    if b.answered_questions:
        console.print(f"\n[bold green]🎯 旧疑问已有答案[/bold green]")
        for q in b.answered_questions:
            console.print(f"  {escape(q['text'][:56])}")
            console.print(
                f"      [dim]→ #{q['answer_id']}《{escape((q['answer_title'] or '')[:34])}》[/dim]"
            )

    if b.top_content:
        console.print(f"\n[bold]本期高密度内容[/bold]")
        for c in b.top_content[:5]:
            score = f"{c['density']:.0f}" if c["density"] is not None else "-"
            console.print(f"  #{c['id']} {escape(c['title'][:50])} [dim]· {score}[/dim]")

    if b.gaps:
        console.print(f"\n[bold]知识盲区[/bold]")
        for g in b.gaps[:5]:
            console.print(f"  {escape(g['name'])} [dim]提及 {g['count']} 次[/dim]")

    if b.open_questions:
        console.print(f"\n[bold]仍未解决的疑问[/bold]")
        for q in b.open_questions[:5]:
            console.print(f"  ? {escape(q['text'][:60])}")

    if b.my_notes:
        console.print(f"\n[bold]我的批注[/bold]")
        for n in b.my_notes[:5]:
            # 类型标签必须转义：rich 会把 [insight] 当作 markup 标签吃掉
            console.print(
                f"  {escape('[' + n['note_type'] + ']')} {escape(n['text'][:56])}"
            )

    if b.low_value:
        console.print(f"\n[dim]本期以下内容密度偏低，可考虑调整信息源：[/dim]")
        for c in b.low_value:
            console.print(f"  [dim]#{c['id']} {escape(c['title'][:44])} · {c['density']:.0f}[/dim]")

    console.print(f"\n[dim]本月 LLM 花费 ${b.cost_usd:.4f}[/dim]")


@app.command()
def vault(
    out_dir: Optional[str] = typer.Argument(None, help="Obsidian 库目录"),
    no_entities: bool = typer.Option(False, "--no-entities", help="不生成概念页"),
    no_moc: bool = typer.Option(False, "--no-moc", help="不生成总览页"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """导出为 Obsidian 库：关系转双链，概念页反向索引。"""
    cfg = _load(config)
    target = out_dir or cfg.review.vault_dir
    if not target:
        _err(
            "请指定导出目录，或在 config.yaml 设置 review.vault_dir\n"
            "例如：distill vault ~/Obsidian/知识库"
        )
        raise typer.Exit(1)

    from .review import VaultExporter

    with Pipeline(cfg) as pipe:
        exporter = VaultExporter(
            pipe.db, min_entity_mentions=cfg.knowledge.entity_min_mentions
        )
        stats = exporter.export(
            Path(target),
            include_entities=not no_entities,
            include_moc=not no_moc,
            limit=limit,
        )

    console.print(f"已导出到 [cyan]{escape(str(Path(target).expanduser()))}[/cyan]")
    console.print(f"  内容笔记 {stats.notes} 篇")
    if stats.entities:
        console.print(f"  概念页 {stats.entities} 个")
    if stats.mocs:
        console.print("  知识库总览.md")
    if stats.skipped:
        console.print(f"\n[dim]跳过 {len(stats.skipped)} 篇：{escape(stats.skipped[0])} 等[/dim]")
    console.print(
        "\n[dim]在 Obsidian 中打开该目录，用图谱视图可看到知识网络[/dim]"
    )


@app.command()
def questions(
    close: Optional[int] = typer.Option(None, "--close", help="手动标记某疑问已解决"),
    limit: int = typer.Option(30, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """未解疑问追踪。新内容入库时会自动尝试匹配回答。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        if close is not None:
            pipe.db.conn.execute(
                "UPDATE questions SET status = 'closed' WHERE id = ?", (close,)
            )
            pipe.db.conn.commit()
            _ok(f"已关闭疑问 #{close}")
            raise typer.Exit(0)

        rows = pipe.db.open_questions(limit=limit)
        answered = pipe.db.conn.execute(
            "SELECT q.text, q.answered_by_source, s.title FROM questions q "
            "LEFT JOIN sources s ON s.id = q.answered_by_source "
            "WHERE q.status = 'answered' ORDER BY q.id DESC LIMIT 10"
        ).fetchall()

    if answered:
        console.print("[bold green]已找到答案[/bold green]")
        for r in answered:
            console.print(f"  {escape(r['text'][:58])}")
            console.print(
                f"      [dim]→ #{r['answered_by_source']}《{escape((r['title'] or '')[:36])}》[/dim]"
            )
        console.print()

    if not rows:
        console.print("没有未解疑问。")
        return

    console.print(f"[bold]未解疑问 {len(rows)} 条[/bold] [dim](消化新内容时会自动匹配)[/dim]\n")
    for r in rows:
        console.print(f"  #{r['id']} {escape(r['text'][:62])}")
        if r["source_title"]:
            console.print(f"      [dim]来自《{escape(r['source_title'][:40])}》[/dim]")
    console.print(
        "\n[dim]可用 distill find \"…\" 主动找答案，或 --close N 手动关闭[/dim]"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="默认只监听本机"),
    port: int = typer.Option(8765, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="改代码自动重启（开发用）"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="启动后自动打开浏览器"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """启动本地网页版：内容库 / 阅读 / 问答 / 图谱 / 重温 / 简报。"""
    cfg = _load(config)

    try:
        import uvicorn
    except ImportError:
        _err(
            "网页版需要额外依赖，请先安装：\n"
            "  pip install -e '.[web]'\n"
            "（Jinja2 服务端渲染 + 原生 JS，不需要 Node）"
        )
        raise typer.Exit(1)

    from .web import create_app

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    console.print(f"网页版已启动：[cyan]{url}[/cyan]")
    if host == "0.0.0.0":
        # 默认绑本机，改成 0.0.0.0 就暴露到局域网了，且本服务没有鉴权
        console.print(
            "[yellow]注意[/yellow] 监听 0.0.0.0 会让同网段设备都能访问，"
            "且本服务没有鉴权。"
        )
    console.print("[dim]Ctrl+C 停止[/dim]\n")

    if open_browser:
        import threading
        import webbrowser

        # 延迟一点再开，否则页面比服务先到会显示连接失败
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    if reload:
        # reload 模式需要 import 字符串，用环境变量传配置路径
        if config:
            os.environ["DISTILLER_CONFIG"] = config
        if profile:
            os.environ["DISTILLER_PROFILE"] = profile
        uvicorn.run(
            "distiller.web.factory:app", host=host, port=port,
            reload=True, log_level="warning",
        )
    else:
        uvicorn.run(
            create_app(cfg, profile=profile),
            host=host, port=port, log_level="warning",
        )


sub_app = typer.Typer(help="订阅：把自然语言选材固化成定期任务")
app.add_typer(sub_app, name="subscribe")


@sub_app.command("add")
def sub_add(
    name: str = typer.Argument(..., help="订阅名，如 finance-daily"),
    instruction: str = typer.Argument(..., help="自然语言指令"),
    domain: Optional[str] = typer.Option(None, "--domain", "-d"),
    every: int = typer.Option(24, "--every", help="间隔小时数"),
    max_items: int = typer.Option(5, "--max", help="单次最多消化几条"),
    min_density: Optional[float] = typer.Option(
        None, "--min-density", help="低于此密度的内容丢弃，不浪费后续成本"
    ),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """新增订阅。先用 distill plan 确认指令能被正确理解。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        if pipe.db.get_subscription(name):
            _err(f"订阅「{name}」已存在")
            raise typer.Exit(1)

        # 存指令前先解析一次，让用户当场看到工具的理解，避免写错跑一周才发现
        from .discover import DiscoveryEngine

        engine = DiscoveryEngine(cfg, pipe.db)
        plan = engine.plan(instruction)

        sid = pipe.db.add_subscription(
            name, instruction,
            query_ir=plan.query.model_dump(mode="json"),
            domain=domain,
            interval_hours=every,
            max_items=max_items,
            min_density=min_density,
        )

    _ok(f"已创建订阅 #{sid}「{name}」· 每 {every} 小时 · 单次最多 {max_items} 条")
    console.print(f"  [dim]指令：{escape(instruction)}[/dim]")
    if plan.needs_clarify:
        console.print(
            "\n[yellow]注意[/yellow] 该指令存在歧义，"
            "订阅运行时无法反问，会按当前解析执行。"
        )
        console.print(f"  [dim]建议先用 distill plan \"{escape(instruction)}\" 确认[/dim]")
    console.print(f"\n[dim]试跑：[/dim][cyan]distill subscribe run {name} --dry-run[/cyan]")


@sub_app.command("list")
def sub_list(config: Optional[str] = typer.Option(None, "--config")) -> None:
    """列出全部订阅。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        rows = pipe.db.list_subscriptions()
        seen = {int(r["id"]): pipe.db.seen_count(int(r["id"])) for r in rows}

    if not rows:
        console.print("还没有订阅。")
        console.print(
            "\n[dim]创建：[/dim]"
            "[cyan]distill subscribe add finance \"B站 某UP主 理财方向 最近内容\"[/cyan]"
        )
        return

    table = Table(box=None, pad_edge=False)
    for col in ("ID", "名称", "状态", "间隔", "上次运行", "累计入库", "已处理", "花费"):
        table.add_column(col)

    for r in rows:
        status = "[green]启用[/green]" if r["enabled"] else "[dim]停用[/dim]"
        if r["last_status"] == "failed":
            status = "[red]失败[/red]"
        table.add_row(
            str(r["id"]),
            escape(r["name"]),
            status,
            f"{r['interval_hours']}h",
            (r["last_run_at"] or "从未")[:16],
            str(r["total_added"]),
            str(seen.get(int(r["id"]), 0)),
            f"${r['total_cost_usd']:.3f}",
        )
    console.print(table)

    failed = [r for r in rows if r["last_status"] == "failed"]
    for r in failed:
        console.print(f"\n[red]#{r['id']} {escape(r['name'])}[/red]：{escape(r['last_error'] or '')}")


@sub_app.command("run")
def sub_run(
    name: Optional[str] = typer.Argument(None, help="订阅名或 ID；省略则跑所有到期的"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只看会抓什么，不消化"),
    force: bool = typer.Option(False, "--force", help="忽略间隔，立即运行"),
    export: Optional[str] = typer.Option(None, "--export", help="导出日报 Markdown"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """运行订阅。适合挂到系统定时任务里。"""
    cfg = _load(config)
    from .subscribe import SubscriptionRunner, render_digest_markdown

    runner = SubscriptionRunner(
        cfg, profile=profile,
        progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]"),
    )

    if name:
        with Pipeline(cfg) as pipe:
            sub = pipe.db.get_subscription(name)
            if sub is None:
                _err(f"找不到订阅「{name}」")
                raise typer.Exit(1)
            sid = int(sub["id"])
        results = [runner.run_one(sid, dry_run=dry_run)]
    elif force:
        with Pipeline(cfg) as pipe:
            ids = [int(r["id"]) for r in pipe.db.list_subscriptions(enabled_only=True)]
        results = [runner.run_one(i, dry_run=dry_run) for i in ids]
    else:
        results = runner.run_due(dry_run=dry_run)

    if not results:
        console.print("没有到期的订阅。[dim]用 --force 可立即运行全部[/dim]")
        return

    for r in results:
        if r.status == "failed":
            console.print(f"[red]✗[/red] {escape(r.name)}：{escape(r.error or '')}")
            continue
        if r.status == "skipped":
            console.print(f"[yellow]○[/yellow] {escape(r.name)}：{escape(r.error or '')}")
            continue

        head = "预演" if dry_run else "完成"
        console.print(f"[green]✓[/green] {escape(r.name)} {head}：{escape(r.note())}")
        for item in r.added_items:
            score = (
                f" [dim]· 密度 {item['density']:.0f}[/dim]"
                if item.get("density") is not None
                else ""
            )
            sid = item.get("source_id")
            prefix = f"#{sid} " if sid else ""
            console.print(f"    {prefix}{escape(str(item['title'])[:52])}{score}")

    if export and not dry_run:
        with Pipeline(cfg) as pipe:
            month = pipe.db.month_cost()
        path = Path(export).expanduser()
        if path.is_dir():
            from datetime import date

            path = path / f"订阅日报-{date.today().isoformat()}.md"
        write_text(path, render_digest_markdown(results, cost_month=month))
        _ok(f"已导出 {path}")


@sub_app.command("toggle")
def sub_toggle(
    name: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """启用/停用订阅。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        sub = pipe.db.get_subscription(name)
        if sub is None:
            _err(f"找不到订阅「{name}」")
            raise typer.Exit(1)
        new_state = not bool(sub["enabled"])
        pipe.db.set_subscription_enabled(int(sub["id"]), new_state)
    _ok(f"订阅「{name}」已{'启用' if new_state else '停用'}")


@sub_app.command("remove")
def sub_remove(
    name: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """删除订阅。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        sub = pipe.db.get_subscription(name)
        if sub is None:
            _err(f"找不到订阅「{name}」")
            raise typer.Exit(1)
        pipe.db.delete_subscription(int(sub["id"]))
    _ok(f"已删除订阅「{name}」")


@sub_app.command("history")
def sub_history(
    name: str = typer.Argument(...),
    limit: int = typer.Option(10, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看运行历史，用于排查「为什么这条没抓到」。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        sub = pipe.db.get_subscription(name)
        if sub is None:
            _err(f"找不到订阅「{name}」")
            raise typer.Exit(1)
        runs = pipe.db.subscription_runs(int(sub["id"]), limit=limit)

    console.print(f"[bold]{escape(sub['name'])}[/bold] [dim]{escape(sub['instruction'])}[/dim]\n")
    if not runs:
        console.print("还没有运行记录。")
        return
    for r in runs:
        mark = {"ok": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(
            r["status"], "[yellow]○[/yellow]"
        )
        console.print(f"{mark} {r['started_at'][:16]} · {escape(r['note'] or r['status'])}")


@app.command(name="domain")
def domain_cmd(
    action: str = typer.Argument("list", help="list / show / template"),
    name: Optional[str] = typer.Argument(None, help="领域名，如 finance"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看领域分区与 agent 规则。理财领域建议配时效与权威度加权。"""
    cfg = _load(config)

    if action == "template":
        # 直接打印可粘贴的配置片段，比让用户查文档快
        console.print(
            """[dim]# 粘贴到 config.yaml 的 agent.domains 下[/dim]
[cyan]agent:
  domains:
    finance:
      extract_numeric: true          # 强制抽取数字，供 agent 计算
      require_as_of: true            # 无时间标注的结论降可信度
      default_max_staleness_days: 180
      authority_weights:
        official: 1.0                # 央行、交易所、监管
        peer_reviewed: 0.9
        media: 0.6
        self_media: 0.4              # 自媒体理财内容大幅降权
      disclaimer: true
      disclaimer_text: >-
        以下内容来自个人知识库的信息整理，不构成投资建议。
        请注意区分事实与观点，并自行核实时效性。[/cyan]"""
        )
        return

    with Pipeline(cfg) as pipe:
        from .agent import AgentService

        svc = AgentService(pipe.db, cfg)

        if action == "list":
            rows = pipe.db.conn.execute(
                "SELECT domain, COUNT(*) AS claims, COUNT(DISTINCT source_id) AS srcs "
                "FROM claims WHERE domain IS NOT NULL GROUP BY domain ORDER BY claims DESC"
            ).fetchall()
            configured = set((cfg.agent.domains or {}).keys())

            if not rows and not configured:
                console.print("还没有领域分区。消化内容时会自动标注领域。")
                console.print("\n[dim]查看理财领域配置模板：[/dim][cyan]distill domain template[/cyan]")
                return

            table = Table(box=None, pad_edge=False)
            for col in ("领域", "内容数", "知识单元", "已配置规则", "默认时效"):
                table.add_column(col)
            seen = set()
            for r in rows:
                d = r["domain"]
                seen.add(d)
                dcfg = svc.domain_config(d)
                table.add_row(
                    d, str(r["srcs"]), str(r["claims"]),
                    "是" if d in configured else "[dim]否[/dim]",
                    f"{dcfg['default_max_staleness_days']} 天"
                    if dcfg["default_max_staleness_days"]
                    else "[dim]未设[/dim]",
                )
            for d in sorted(configured - seen):
                table.add_row(d, "0", "0", "是", "-")
            console.print(table)
            return

        if action == "show":
            if not name:
                _err("请指定领域名")
                raise typer.Exit(1)
            stats = svc.stats(name)
            console.print(f"[bold]{escape(name)}[/bold]\n")
            console.print(f"内容 {stats.source_count} 篇 · 知识单元 {stats.claim_count} 条")
            console.print(f"覆盖度 [bold]{stats.sufficiency.value}[/bold] · 冲突 {stats.conflict_count} 处")
            if stats.stale_ratio:
                console.print(f"陈旧占比 {stats.stale_ratio:.0%} [dim](超出该领域默认时效)[/dim]")
            if stats.by_claim_type:
                console.print(f"\n[bold]类型分布[/bold]")
                for k, v in sorted(stats.by_claim_type.items(), key=lambda x: -x[1]):
                    console.print(f"  {k:12s} {v}")
            if stats.by_authority:
                console.print(f"\n[bold]来源权威度[/bold]")
                for k, v in sorted(stats.by_authority.items(), key=lambda x: -x[1]):
                    console.print(f"  {k:14s} {v}")
            if stats.top_subjects:
                console.print(f"\n[bold]高频主题[/bold]")
                for s in stats.top_subjects[:8]:
                    console.print(f"  {escape(str(s['subject'])[:40])} [dim]{s['count']}[/dim]")
            if stats.disclaimer:
                console.print(f"\n[dim]{escape(stats.disclaimer)}[/dim]")
            return

    _err(f"未知操作「{action}」，可选：list / show / template")
    raise typer.Exit(1)


@app.command(name="serve-agent")
def serve_agent(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port", "-p"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """启动 Agent HTTP API（/v1）。供外部 agent 跨语言调用。"""
    cfg = _load(config)
    bind = host or cfg.agent.host
    bind_port = port or cfg.agent.port

    try:
        import uvicorn
    except ImportError:
        _err("需要额外依赖：pip install -e '.[agent]'")
        raise typer.Exit(1)

    # 暴露到非本机却没设 token，等于把知识库公开。拒绝启动而非默默放行。
    if bind not in ("127.0.0.1", "localhost", "::1") and not (
        cfg.agent.read_token or cfg.agent.write_token
    ):
        _err(
            f"拒绝在 {bind} 上启动：未配置访问令牌。\n"
            "请在 config.yaml 设置 agent.read_token，或改用默认的 127.0.0.1。"
        )
        raise typer.Exit(1)

    from .agent.http_api import create_agent_app

    console.print(f"Agent API 已启动：[cyan]http://{bind}:{bind_port}/v1[/cyan]")
    console.print(f"接口文档：[cyan]http://{bind}:{bind_port}/v1/docs[/cyan]")
    console.print(
        f"[dim]写接口：{'已开启' if cfg.agent.allow_write else '已关闭'} · "
        f"令牌：{'已配置' if cfg.agent.read_token else '未配置（仅本机可访问）'}[/dim]"
    )
    console.print("[dim]Ctrl+C 停止[/dim]\n")

    uvicorn.run(
        create_agent_app(cfg, profile=profile),
        host=bind, port=bind_port, log_level="warning",
    )


@app.command(name="serve-mcp")
def serve_mcp(
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
    show_tools: bool = typer.Option(False, "--tools", help="只列出工具清单后退出"),
) -> None:
    """启动 MCP Server（stdio）。供 Claude Desktop 等客户端接入。"""
    cfg = _load(config)

    try:
        from .agent.mcp_server import build_server, run_stdio
    except RuntimeError as exc:
        _err(str(exc))
        raise typer.Exit(1)

    if show_tools:
        try:
            server, names = build_server(cfg, profile=profile)
        except RuntimeError as exc:
            _err(str(exc))
            raise typer.Exit(1)

        import asyncio

        tools = asyncio.run(server.list_tools())
        console.print(f"[bold]MCP 工具清单[/bold] [dim]({len(tools)} 个)[/dim]\n")
        for t in tools:
            readonly = getattr(t.annotations, "read_only_hint", None) if t.annotations else None
            mark = "[green]只读[/green]" if readonly else "[yellow]可写[/yellow]"
            has_out = (
                "[dim]· 强类型输出[/dim]"
                if getattr(t, "output_schema", None)
                else ""
            )
            console.print(f"  {t.name:20s} {mark} {has_out}")
            console.print(f"    [dim]{escape((t.description or '')[:92])}[/dim]")
        console.print(
            f"\n[dim]写接口：{'已开启' if cfg.agent.allow_write else '已关闭（kb_ingest 不暴露）'}[/dim]"
        )
        return

    import asyncio

    # stdio 模式下 stdout 是协议通道，任何打印都会破坏协议，因此不输出
    asyncio.run(run_stdio(cfg, profile=profile))


@app.command(name="platforms")
def platforms_cmd(
    test_url: Optional[str] = typer.Option(
        None, "--test", help="测试某个 URL 会由哪个采集器处理"
    ),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """查看各平台采集器状态与声明式站点规则。"""
    cfg = _load(config)
    icfg = cfg.ingest

    if test_url:
        from .ingest import pick_adapter

        try:
            ad = pick_adapter(test_url, icfg)
        except FetchError as exc:
            _err(str(exc))
            raise typer.Exit(1)
        kind = "声明式规则" if ad.priority == 50 else "内置采集器"
        _ok(f"将由「{ad.name}」处理（{kind}，priority={ad.priority}）")
        p = icfg.platform(ad.name)
        eff = icfg.effective(ad.name)
        console.print(f"  cookie   : {eff.cookies_from_browser or '未配置'}")
        console.print(f"  限流     : {p.rate_limit_sec or 0} 秒")
        console.print(f"  代理     : {eff.proxy or '无'}")
        return

    from .ingest.base import _ADAPTERS

    table = Table(box=None, pad_edge=False)
    for col in ("采集器", "类型", "状态", "cookie", "限流", "说明"):
        table.add_column(col)

    notes = {
        "bilibili": "字幕需登录态，否则全部走转写",
        "wechat": "图片型文章需 [ocr]",
        "xhs": "风控严，建议配 cookie 并限流",
        "zhihu": "走私有 API",
        "web": "通用兜底，trafilatura 抽取",
    }

    for cls in sorted(_ADAPTERS, key=lambda c: c.priority):
        p = icfg.platform(cls.name)
        eff = icfg.effective(cls.name)
        table.add_row(
            cls.name,
            "内置",
            "[green]启用[/green]" if p.enabled else "[dim]已禁用[/dim]",
            eff.cookies_from_browser or "[dim]-[/dim]",
            f"{p.rate_limit_sec}s" if p.rate_limit_sec else "[dim]-[/dim]",
            f"[dim]{escape(notes.get(cls.name, ''))}[/dim]",
        )

    for rule in icfg.sites:
        p = icfg.platform(rule.name)
        table.add_row(
            escape(rule.name),
            "声明式",
            "[green]启用[/green]" if (rule.enabled and p.enabled) else "[dim]已禁用[/dim]",
            icfg.effective(rule.name).cookies_from_browser or "[dim]-[/dim]",
            f"{p.rate_limit_sec}s" if p.rate_limit_sec else "[dim]-[/dim]",
            f"[dim]{escape(', '.join(rule.match)[:34])}[/dim]",
        )

    console.print(table)

    if not icfg.sites:
        console.print(
            "\n[dim]声明式站点规则可在 config.yaml 的 ingest.sites 下添加，"
            "适用于正文在固定 DOM 节点的普通站点。[/dim]"
        )
    console.print(
        "\n[dim]测试某链接走哪个采集器：[/dim]"
        "[cyan]distill platforms --test <url>[/cyan]"
    )


def _probe_browser_cookie(browser: str) -> tuple[bool, str]:
    """实际读一次 cookie，确认登录态可用。

    只检查包是否安装不够：浏览器没登录、profile 路径变了、
    macOS 钥匙串拒绝授权，都会让 cookie 为空而没有任何提示。
    """
    try:
        import browser_cookie3

        loader = getattr(browser_cookie3, browser, None)
        if loader is None:
            return False, f"{browser} 不是受支持的浏览器"
        jar = list(loader(domain_name="bilibili.com"))
        if not jar:
            return False, f"{browser} 已安装但未读到 bilibili cookie（未登录？）"
        has_session = any(c.name == "SESSDATA" for c in jar)
        if not has_session:
            return False, f"{browser} 读到 {len(jar)} 个 cookie，但缺 SESSDATA（未登录）"
        return True, f"{browser} · {len(jar)} 个 cookie · 含登录态"
    except Exception as exc:  # noqa: BLE001
        return False, f"{browser} 读取失败：{str(exc)[:60]}"


@app.command(name="distill-pending")
def distill_pending(
    series: Optional[str] = typer.Option(None, "--series", help="按系列名筛选"),
    ids: Optional[str] = typer.Option(None, "--ids", help="指定编号，如 2-13"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多处理多少条"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """给已入库但还没浓缩的内容补浓缩。

    用于「先批量采集、后配 LLM」的场景：正文已在库里，
    不必重新下载或转写，只跑浓缩这一步。
    """
    cfg = _load(config)

    with Pipeline(
        cfg, profile=profile, progress=lambda m: console.print(f"[dim]  {escape(m)}[/dim]")
    ) as pipe:
        sql = (
            "SELECT s.id, s.title FROM sources s "
            "WHERE NOT EXISTS (SELECT 1 FROM digests d WHERE d.source_id = s.id) "
            "  AND EXISTS (SELECT 1 FROM segments g WHERE g.source_id = s.id)"
        )
        args: list = []
        if series:
            sql += " AND s.title LIKE ?"
            args.append(f"%{series}%")
        if ids:
            picked = _resolve_qa_ids(pipe, ids=ids, series=None, author=None, limit=None)
            if picked:
                sql += f" AND s.id IN ({','.join('?' * len(picked))})"
                args.extend(picked)
        sql += " ORDER BY s.id"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)

        rows = pipe.db.conn.execute(sql, args).fetchall()
        if not rows:
            _ok("没有待浓缩的内容（都已浓缩过，或还没采集正文）。")
            return

        console.print(f"\n待浓缩 [bold]{len(rows)}[/bold] 条")
        used = pipe.db.month_cost()
        console.print(
            f"[dim]本月已用 ${used:.4f} / ${cfg.limits.monthly_budget_usd}[/dim]"
        )
        if not yes and not typer.confirm(f"开始浓缩 {len(rows)} 条？", default=True):
            console.print("已取消。")
            raise typer.Exit(0)

        ok = failed = 0
        started = time.monotonic()
        for i, row in enumerate(rows, start=1):
            eta = ""
            if i > 3:
                per = (time.monotonic() - started) / (i - 1)
                eta = f"  [dim]剩余约 {_fmt_duration(per * (len(rows) - i + 1))}[/dim]"
            console.print(f"\n[cyan][{i}/{len(rows)}][/cyan]{eta}")
            console.print(f"  {escape(str(row['title'])[:70])}")
            try:
                # 复用库内正文，不重新采集
                pipe.redistill(int(row["id"]))
                ok += 1
            except KeyboardInterrupt:
                console.print("\n[yellow]已中断[/yellow]，已完成的都已入库，重跑可续。")
                break
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [yellow]失败[/yellow]：{escape(str(exc)[:110])}")
                failed += 1

        console.print()
        _ok(
            f"完成：成功 {ok} 条"
            + (f"，失败 {failed} 条" if failed else "")
            + f" · 用时 {_fmt_duration(time.monotonic() - started)}"
            + f" · 花费 ${pipe.db.month_cost() - used:.4f}"
        )
        console.print("导出：[cyan]distill qa-export out.md --series \"…\"[/cyan]")


@app.command(name="qa-export")
def qa_export(
    out: Optional[str] = typer.Argument(None, help="输出文件路径，省略则打印到终端"),
    ids: Optional[str] = typer.Option(None, "--ids", help="指定内容编号，如 2-13 或 2,5,7"),
    series: Optional[str] = typer.Option(None, "--series", help="按系列名筛选，匹配标题"),
    author: Optional[str] = typer.Option(None, "--author", help="按作者筛选"),
    title: Optional[str] = typer.Option(None, "--title", help="文档标题"),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n", help="最多导出多少条（默认不限）"
    ),
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """导出 Q&A 合集：把问答体系列内容汇成「问题→要点→出处」的文档。

    适合「每天一个宝藏问题」「XX 十问」这类标题即问题的系列。
    未配置 LLM 时退化为原文节选，并在文档中明确标注。
    """
    cfg = _load(config)
    from .review.qa import build_collection, render_markdown

    with Pipeline(cfg) as pipe:
        source_ids = _resolve_qa_ids(pipe, ids=ids, series=series, author=author, limit=limit)
        if not source_ids:
            _err("没有匹配的内容。可先用 distill list 查看库内编号。")
            raise typer.Exit(1)

        doc_title = title or (f"{series} · Q&A 合集" if series else "Q&A 合集")
        coll = build_collection(pipe.db, source_ids, title=doc_title)
        text = render_markdown(coll)

    if out:
        path = Path(out).expanduser()
        write_text(path, text)
        _ok(f"已导出 {coll.total} 题 → {path}")
        if coll.extractive_count:
            console.print(
                f"[yellow]![/yellow] 其中 {coll.extractive_count} 题为原文节选"
                "（未配置 LLM）。配好 LLM 后重跑 distill add --force 可得模型摘要。"
            )
    else:
        console.print(text)


def _resolve_qa_ids(
    pipe, *, ids: str | None, series: str | None, author: str | None, limit: int | None
) -> list[int]:
    """解析导出范围。三种方式可组合，都不给则取全库。

    limit=None 表示不限。默认不限是有意的：
    此前默认 200，导出 211 题的系列会**静默少 11 题**，
    而用户完全看不出来——这类"看起来成功了"的截断最难发现。
    """
    if ids:
        picked: list[int] = []
        for part in ids.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                if a.strip().isdigit() and b.strip().isdigit():
                    picked.extend(range(int(a), int(b) + 1))
            elif part.isdigit():
                picked.append(int(part))
        return picked[:limit] if limit else picked

    sql = "SELECT id FROM sources WHERE 1=1"
    args: list = []
    if series:
        sql += " AND title LIKE ?"
        args.append(f"%{series}%")
    if author:
        sql += " AND author LIKE ?"
        args.append(f"%{author}%")
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [int(r["id"]) for r in pipe.db.conn.execute(sql, args).fetchall()]


@app.command()
def doctor(config: Optional[str] = typer.Option(None, "--config")) -> None:
    """环境自检。迁移到新平台后第一件事就是跑它。"""
    caps = detect_caps()
    console.print(f"[bold]平台[/bold]      : {caps.os_name} {caps.os_version} ({caps.arch})")
    console.print(f"[bold]Python[/bold]    : {caps.python_version}")
    console.print("[bold]编码[/bold]      : UTF-8 强制（已封装 IO 层）")

    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        console.print(f"[bold]数据目录[/bold]  : {pipe.paths.root}")
        console.print(f"[bold]数据库[/bold]    : {pipe.paths.db.name} · schema v{pipe.db.get_meta('schema_version')}")
        console.print(f"[bold]本月花费[/bold]  : ${pipe.db.month_cost():.4f} / ${cfg.limits.monthly_budget_usd}")
        indexed = pipe.db.count_indexed(cfg.retrieval.embedding_model)
        pending = len(pipe.db.sources_without_index(cfg.retrieval.embedding_model))
        console.print(
            f"[bold]检索索引[/bold]  : {indexed} 块已索引"
            + (f" · [yellow]{pending} 篇待补建[/yellow]" if pending else "")
        )
        if pipe.db.fts_needs_rebuild():
            # 静默失效最危险：关键词那一路不报错，只是召回变差
            console.print(
                "[yellow]![/yellow] 关键词索引待重建，中文检索目前只有语义一路："
                "[cyan]distill index --rebuild-fts[/cyan]"
            )

    console.print(f"[bold]采集器[/bold]    : {', '.join(list_adapters(cfg.ingest))}")

    # cookie 链路必须连着配置一起看：只报「装没装」没有意义，
    # 关键是「配了却用不上」这种静默失效——B 站投稿列表会直接返回 -352，
    # 而用户以为登录态已经生效。
    browser = cfg.ingest.cookies_from_browser
    if browser:
        if not caps.has_browser_cookie:
            console.print(
                f"[bold]浏览器Cookie[/bold]: [red]配置了 {browser} 但未安装 "
                f"browser_cookie3，cookie 会被静默忽略[/red]"
            )
        else:
            ok, detail = _probe_browser_cookie(browser)
            style = "green" if ok else "yellow"
            console.print(f"[bold]浏览器Cookie[/bold]: [{style}]{detail}[/{style}]")

    ocr = caps.ocr_default or "无"
    ocr_style = "green" if caps.ocr_default else "yellow"
    console.print(f"[bold]OCR[/bold]       : [{ocr_style}]{ocr}[/{ocr_style}]"
                  f"  (可用: {', '.join(caps.ocr_engines) or '无'})")
    # 转写是一条链：ASR 引擎 + 下载器 + ffmpeg，缺一环都跑不通。
    # 分开列会让人误判可用性，所以先给结论再列明细。
    asr_names = ", ".join(caps.asr_engines) or "未安装"
    if caps.can_transcribe:
        console.print(f"[bold]ASR[/bold]       : [green]{asr_names}[/green]")
    elif caps.asr_engines:
        console.print(
            f"[bold]ASR[/bold]       : [yellow]{asr_names}（链路不完整，无字幕视频仍无法转写）[/yellow]"
        )
    else:
        console.print(f"[bold]ASR[/bold]       : [yellow]{asr_names}[/yellow]")

    console.print(
        f"[bold]音频下载[/bold]  : {'✓ yt-dlp' if caps.has_downloader else '✗ 缺 yt-dlp（音视频转写需要）'}"
    )
    console.print(f"[bold]ffmpeg[/bold]    : {'✓' if caps.has_ffmpeg else '✗（音视频转写需要）'}")

    from .compat.device import detect_device

    dev = detect_device()
    console.print(f"[bold]计算设备[/bold]  : {dev.device}/{dev.compute_type} [dim]({dev.note})[/dim]")
    console.print(f"[bold]向量模型[/bold]  : {cfg.retrieval.embedding_model}"
                  f" [dim](混合检索: {'开' if cfg.retrieval.hybrid else '关'})[/dim]")

    console.print("\n[bold]LLM Provider[/bold]")
    for name, pc in cfg.providers.items():
        try:
            provider = build_provider(name, pc)
            healthy, reason = provider.health()
        except Exception as exc:  # noqa: BLE001
            healthy, reason = False, str(exc)
        mark = "[green]✓[/green]" if healthy else "[yellow]✗[/yellow]"
        detail = "" if healthy else f" [dim]({reason})[/dim]"
        console.print(f"  {mark} {name} [dim]{pc.type}[/dim]{detail}")

    for note in caps.notes:
        console.print(f"[yellow]![/yellow] {escape(note)}")


@app.command()
def cost(
    config: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """本月 LLM 花费。"""
    cfg = _load(config)
    with Pipeline(cfg) as pipe:
        used = pipe.db.month_cost()
        rows = pipe.db.conn.execute(
            "SELECT provider, model, COUNT(*) n, SUM(tokens_in) ti, "
            "SUM(tokens_out) tokens_out, SUM(cost_usd) c FROM usage_log "
            "WHERE created_at >= date('now','start of month') GROUP BY provider, model"
        ).fetchall()

    console.print(f"本月合计 [bold]${used:.4f}[/bold] / 预算 ${cfg.limits.monthly_budget_usd}\n")
    if not rows:
        console.print("[dim]本月暂无调用记录[/dim]")
        return
    table = Table()
    table.add_column("Provider")
    table.add_column("模型")
    table.add_column("次数", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("花费", justify="right")
    for r in rows:
        table.add_row(
            r["provider"], r["model"], str(r["n"]),
            f"{r['ti']}/{r['tokens_out']}", f"${r['c']:.4f}",
        )
    console.print(table)


if __name__ == "__main__":
    app()

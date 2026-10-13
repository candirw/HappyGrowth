"""流程编排：ingest → (transcribe) → distill → store。

各层只依赖数据模型不互相引用实现，因此换 ASR、换向量库、换 LLM 都只动一层。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .compat.paths import DataPaths, get_data_paths, safe_filename
from .compat.fsio import write_text
from .config import Config
from .distill import Summarizer
from .ingest import FetchError, PasteAdapter, pick_adapter
from .llm import LLMRouter
from .models import ContentType, Digest, RawContent, Source, SourceStatus
from .render.markdown import render_digest
from .review.qa import is_qa_title
from .store import Database

ProgressFn = Callable[[str], None]


class SubtitleMissing(FetchError):
    """--subtitle-only 模式下遇到无字幕内容。

    继承 FetchError 以便现有的错误处理仍然生效，但单独成类，
    让调用方能把「按预期跳过」与「真的失败」分开统计——
    批量导入 200 条时，这两者混在一起报「失败 30 条」会让人以为出了问题。
    """


@dataclass
class IngestResult:
    source: Source
    digest: Digest | None
    raw: RawContent
    md_path: str | None
    reused: bool = False
    indexed_chunks: int = 0
    entities: int = 0
    claims: int = 0
    findings: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    review_note: str | None = None


class Pipeline:
    def __init__(
        self,
        config: Config,
        *,
        profile: str | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.progress = progress or (lambda _m: None)
        self.paths: DataPaths = get_data_paths(
            config.storage.data_dir, use_system_dir=config.storage.use_system_dir
        )
        self.paths.ensure()
        self.db = Database(self.paths.db)
        self._academic = None

    # ---------- 采集 ----------

    def fetch(self, url: str, *, subtitle_only: bool = False) -> RawContent:
        """采集单条内容。

        subtitle_only=True 时，无字幕的音视频**直接跳过而不启动 ASR**。
        批量导入长系列时这是必要的：一条无字幕视频会占用几分钟本地算力，
        200 条里混进几条就能把整个任务拖住，而且从外部完全看不出在干什么。
        """
        # 论文走独立链路：需要学术 API 缓存与分节解析（详见 PAPERS.md）
        from .paper.ingest import is_paper_url

        if is_paper_url(url):
            raw, _ref, _meta = self.fetch_paper(url)
            return raw

        adapter = pick_adapter(url, self.config.ingest)
        self.progress(f"使用 {adapter.name} 采集器")
        raw = adapter.fetch(url)
        self.progress(f"已取得「{raw.title}」· {raw.char_count} 字 · {len(raw.segments)} 段")

        # 无字幕的音视频在此补转写（M2）
        raw = self._maybe_transcribe(raw, subtitle_only=subtitle_only)

        for w in raw.warnings:
            self.progress(f"  提示：{w}")
        return raw

    # ---------- 论文（M3） ----------

    def academic_client(self):
        """学术 API 客户端。缓存与主库分离，可独立清理。"""
        if self._academic is None:
            from .paper import AcademicClient, ApiCache

            cache = ApiCache(
                self.paths.cache / "academic.db",
                ttl_days=self.config.paper.cache_ttl_days,
            )
            self._academic = AcademicClient(
                cache,
                mailto=self.config.paper.mailto,
                progress=self.progress,
            )
        return self._academic

    def fetch_paper(self, text: str):
        """采集论文，返回 (RawContent, PaperRef, PaperMeta)。"""
        from .paper.ingest import ingest_paper

        result = ingest_paper(
            text,
            client=self.academic_client(),
            paths=self.paths,
            progress=self.progress,
            fulltext=self.config.paper.fulltext,
        )
        raw = result[0]
        self.progress(
            f"已取得「{raw.title[:50]}」· {raw.char_count} 字 · {len(raw.segments)} 段"
        )
        for w in raw.warnings:
            self.progress(f"  提示：{w}")
        return result

    def _maybe_transcribe(
        self, raw: RawContent, *, subtitle_only: bool = False
    ) -> RawContent:
        """音视频且无字幕时走 ASR。文章类直接跳过。"""
        if raw.content_type not in (ContentType.VIDEO, ContentType.AUDIO):
            return raw

        from .transcribe import Transcriber

        transcriber = Transcriber(self.config, self.paths, progress=self.progress)
        if not transcriber.needs_asr(raw):
            return raw

        if subtitle_only:
            raise SubtitleMissing(
                f"「{raw.title[:40]}」无字幕，已按 --subtitle-only 跳过"
            )

        # 转写很慢（约为音频时长的 1/12），不打招呼就静默跑起来会让人
        # 以为程序卡死——我自己就误判过一次，用 lsof 才看出在跑 PyAV。
        mins = (raw.duration_sec or 0) / 60
        est = f"，预计 {max(1, round(mins / 12))}~{max(2, round(mins / 6))} 分钟" if mins else ""
        self.progress(
            f"⏳ 无字幕，开始本地转写{est}（CPU 占用会很高；"
            f"加 --subtitle-only 可跳过无字幕内容）"
        )
        return transcriber.ensure_transcript(raw)

    def redistill(self, source_id: int) -> IngestResult:
        """用库内已有正文重跑浓缩，不重新采集。

        场景：先批量采集（无 LLM 时也能入库），之后配好 LLM 再补浓缩。
        重新采集的代价很高——B 站限流、字幕接口可能失效、
        无字幕的还要重跑转写，而正文早就存好了。
        """
        source = self.db.get_source(source_id)
        if source is None:
            raise FetchError(f"内容 #{source_id} 不存在")

        segments = self.db.get_segments(source_id)
        if not segments:
            raise FetchError(
                f"内容 #{source_id} 没有正文，无法浓缩。"
                f"请重新采集：distill add {source.url}"
            )

        raw = RawContent(
            url=source.url,
            content_type=source.content_type,
            platform=source.platform,
            title=source.title,
            author=source.author,
            publish_at=source.publish_at,
            duration_sec=source.duration_sec,
            lang=source.lang,
            segments=segments,
        )
        # force=True：库里已有 source 记录，不加会被当成「已消化」直接复用
        return self.process(raw, force=True)

    def from_paste(self, text: str, *, title: str | None = None) -> RawContent:
        return PasteAdapter(self.config.ingest).from_text(text, title=title)

    def from_file(self, path: str) -> RawContent:
        return PasteAdapter(self.config.ingest).from_file(path)

    # 本地音视频后缀。列全一些——用户不该因为文件是 .opus 就被拒。
    AUDIO_SUFFIXES = {
        ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus",
        ".wma", ".aiff", ".aif", ".amr", ".m4b",
    }
    VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v"}

    @classmethod
    def is_media_file(cls, path: str | Path) -> bool:
        return Path(path).suffix.lower() in (cls.AUDIO_SUFFIXES | cls.VIDEO_SUFFIXES)

    def from_media(self, path: str, *, title: str | None = None) -> RawContent:
        """转写本地录音/视频文件。

        引擎本就支持直接吃文件路径，这里补的是入口——
        此前 `--file` 只读文本，丢个 mp3 进去会报「没有可用的采集器」，
        完全看不出真正原因。

        视频文件先用 ffmpeg 抽音轨：Whisper 只认音频，
        直接喂视频会失败或极慢。
        """
        src = Path(path).expanduser()
        if not src.exists():
            raise FetchError(f"文件不存在：{src}")

        suffix = src.suffix.lower()
        if suffix not in (self.AUDIO_SUFFIXES | self.VIDEO_SUFFIXES):
            raise FetchError(
                f"不支持的媒体格式「{suffix or '无后缀'}」。\n"
                f"  音频：{', '.join(sorted(self.AUDIO_SUFFIXES))}\n"
                f"  视频：{', '.join(sorted(self.VIDEO_SUFFIXES))}"
            )

        from .compat.caps import detect_caps

        caps = detect_caps()
        if not caps.asr_engines:
            raise FetchError(
                '未安装转写引擎，无法处理音视频文件：pip install -e ".[av]"'
            )
        if not caps.has_ffmpeg:
            raise FetchError(
                'ffmpeg 不可用，无法解码音频：pip install -e ".[av]"'
            )

        audio_path = src
        if suffix in self.VIDEO_SUFFIXES:
            self.progress("从视频抽取音轨")
            audio_path = self._extract_audio(src)

        from .transcribe import ASRError, ASROptions, WhisperEngine

        tcfg = self.config.transcribe
        engine = WhisperEngine(
            ASROptions(
                model=tcfg.model,
                language=tcfg.language,
                compute_type=tcfg.compute_type,
                # device 不从配置读：由 compat.device 按平台自动探测
                # （cuda → mps → cpu），避免用户手填出不兼容组合
                vad=tcfg.vad,
                hotwords=list(tcfg.hotwords or []),
            ),
            progress=self.progress,
        )

        self.progress(f"开始转写（{tcfg.model}，首次会下载模型）")
        try:
            segments, lang = engine.transcribe(audio_path)
        except ASRError as exc:
            raise FetchError(f"转写失败：{exc}") from exc

        if not segments:
            raise FetchError(
                "转写结果为空。可能是静音文件，或语言未被识别。"
                "可在 config.yaml 显式设置 transcribe.language。"
            )

        # 用绝对路径的稳定哈希做 URL，便于重复导入时命中去重。
        # 不直接用路径：库内不存绝对路径（跨平台设计约束）。
        digest = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
        return RawContent(
            url=f"localmedia://{digest}/{src.name}",
            content_type=(
                ContentType.VIDEO if suffix in self.VIDEO_SUFFIXES else ContentType.AUDIO
            ),
            platform="local",
            title=title or src.stem,
            segments=segments,
            lang=lang,
            duration_sec=(segments[-1].end_ms // 1000) if segments[-1].end_ms else None,
            warnings=[f"内容由本地文件「{src.name}」转写导入"],
        )

    def _extract_audio(self, src: Path) -> Path:
        """抽音轨为 16k 单声道 wav —— Whisper 的输入要求。"""
        from .compat.binaries import find_ffmpeg, run

        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise FetchError('ffmpeg 不可用：pip install -e ".[av]"')

        out = self.paths.audio / f"{src.stem}-{id(src) & 0xFFFF:04x}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = run(
            [
                str(ffmpeg), "-y", "-i", str(src),
                "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out),
            ],
            check=False,
        )
        if proc.returncode != 0 or not out.exists():
            tail = (proc.stderr or "")[-300:]
            raise FetchError(f"音轨抽取失败：{tail or '未知错误'}")
        return out

    # ---------- 完整流程 ----------

    def process(
        self, raw: RawContent, *, force: bool = False, distill: bool = True
    ) -> IngestResult:
        existing = self.db.find_source_by_url(raw.url)
        if existing and existing.status == SourceStatus.DISTILLED and not force:
            digest = self.db.get_latest_digest(int(existing.id))
            self.progress("该内容已消化过，直接复用（--force 可重跑）")
            return IngestResult(existing, digest, raw, None, reused=True)

        # 无正文时明确失败并给出可行动的建议，而不是让浓缩层抛难懂的异常
        if distill and raw.char_count < 20:
            hint = (
                "音视频转写不可用时可执行 pip install -e \".[av]\" 启用本地 Whisper"
                if raw.content_type in (ContentType.VIDEO, ContentType.AUDIO)
                else "该页面可能需要登录或由 JS 渲染"
            )
            detail = ("；".join(raw.warnings)) if raw.warnings else ""
            raise FetchError(
                f"未获得足够正文（仅 {raw.char_count} 字），无法浓缩。{hint}，"
                f"或用 `distill add \"标题\" --paste` 手动粘贴内容。"
                + (f"\n采集提示：{detail}" if detail else "")
            )

        source_id = self.db.upsert_source(raw, status=SourceStatus.FETCHED)
        self.db.replace_segments(source_id, raw.segments)

        if not distill:
            source = self.db.get_source(source_id)
            assert source is not None
            return IngestResult(source, None, raw, None)

        budget_used = self.db.month_cost()
        if budget_used >= self.config.limits.monthly_budget_usd:
            self.progress(
                f"本月已用 ${budget_used:.2f}，达到预算上限 "
                f"${self.config.limits.monthly_budget_usd}，将尝试降级到本地模型"
            )

        router = LLMRouter(self.config, self.profile)

        # 论文走独立的浓缩 schema（问题/方法/实验/局限），
        # 与视频/文章的分层摘要是两种产物
        if raw.content_type is ContentType.PAPER:
            digest = self._distill_paper(raw, router)
        else:
            summarizer = Summarizer(self.config, router, progress=self.progress)
            try:
                # 问答体内容（标题即问题）走独立 schema：产出「直答 + 要点 +
                # 引申问题」而非通用摘要。用摘要结构装问答，
                # 读者拿到的是一堆片段而不是答案。
                if self.config.distill.qa_mode and is_qa_title(raw.title):
                    self.progress("识别为问答体内容，按问答卡片浓缩")
                    digest = summarizer.run_qa(raw.title, raw.segments)
                else:
                    digest = summarizer.run(raw.title, raw.segments)
            except Exception:
                self.db.update_status(source_id, SourceStatus.FAILED)
                raise

        self.db.save_digest(source_id, digest)
        self.db.set_density(source_id, digest.density_score)
        self.db.update_status(source_id, SourceStatus.DISTILLED)
        self.db.log_usage(
            source_id=source_id,
            task="distill",
            provider=(digest.model_used or "/").split("/")[0],
            model=(digest.model_used or "/").split("/")[-1],
            tokens_in=digest.tokens_in,
            tokens_out=digest.tokens_out,
            cost_usd=digest.cost_usd,
        )

        source = self.db.get_source(source_id)
        assert source is not None
        md_path = self._write_markdown(source, digest)

        # 建检索索引（M2）。失败不影响浓缩结果，只是暂时无法问答。
        indexed = self._try_index(source_id, raw)

        # 知识关联（M3.5）：抽实体/claim + 与历史内容建立关系
        knowledge = self._try_link(source_id, raw, digest, router)

        # 重温入队（M4）。只有值得重温的才入队，详见 ReviewConfig。
        review_note = self._try_enqueue_review(source_id)

        return IngestResult(
            source, digest, raw, md_path,
            indexed_chunks=indexed,
            entities=knowledge.get("entities", 0),
            claims=knowledge.get("claims", 0),
            findings=knowledge.get("findings", []),
            domains=knowledge.get("domains", []),
            review_note=review_note,
        )

    def _try_enqueue_review(self, source_id: int) -> str | None:
        """判定是否加入重温队列。返回给用户看的说明。"""
        if not self.config.review.auto_enqueue:
            return None
        try:
            from .review import ReviewScheduler

            sched = ReviewScheduler(
                self.db,
                min_density=self.config.review.min_density,
                require_value=self.config.review.require_value,
            )
            ok, reasons = sched.enqueue(source_id)
            if ok:
                return "已加入重温队列：" + "；".join(reasons[:2])
            return None
        except Exception as exc:  # noqa: BLE001 - 入队失败不影响消化结果
            self.progress(f"重温入队失败（不影响浓缩）：{exc}")
            return None

    def _try_link(
        self, source_id: int, raw: RawContent, digest: Digest, router: LLMRouter
    ) -> dict:
        """抽取实体与 claim，并与历史内容建立关联。

        整体包在 try 里：知识关联失败不该让已完成的浓缩白做。
        """
        out: dict = {"entities": 0, "claims": 0, "findings": [], "domains": []}
        cfg = self.config.knowledge
        if not cfg.auto_link:
            return out

        try:
            from .knowledge import (
                ClaimExtractor,
                DomainClassifier,
                EntityNormalizer,
                Linker,
                collect_entities,
                format_findings,
                load_user_aliases,
            )

            full_text = "\n".join(s.text for s in raw.segments)
            payload = digest.model_dump() if digest else {}

            # ---- 领域分区 ----
            classifier = DomainClassifier(cfg.domains)
            domains = [name for name, _score in classifier.classify(full_text)]
            if domains:
                self.db.conn.execute(
                    "UPDATE sources SET domains_json = ? WHERE id = ?",
                    (json.dumps(domains, ensure_ascii=False), source_id),
                )
                self.db.conn.commit()
                out["domains"] = domains
                self.progress(f"领域：{'、'.join(domains)}")

            # ---- 实体（零额外 LLM 调用，复用 digest.concepts）----
            if cfg.extract_entities:
                normalizer = EntityNormalizer(load_user_aliases())
                entities = collect_entities(
                    full_text, payload,
                    normalizer=normalizer,
                    max_entities=cfg.max_entities_per_source,
                )
                for entity in entities:
                    eid = self.db.upsert_entity(
                        entity.name,
                        etype=entity.etype,
                        definition=entity.definition,
                        aliases=normalizer.alias_list(entity.name),
                    )
                    self.db.add_mention(eid, source_id)
                out["entities"] = len(entities)

            # ---- claim（仅密度达标的内容）----
            if cfg.extract_claims and (digest.density_score or 0) >= cfg.claim_min_density:
                primary = domains[0] if domains else None
                extractor = ClaimExtractor(router, progress=self.progress)
                claims, warns = extractor.extract(
                    raw.title, raw.segments,
                    domain=primary,
                    rule=classifier.rule(primary) if primary else None,
                    authority=self.db.get_source(source_id).authority.value
                    if self.db.get_source(source_id) else None,
                )
                if claims:
                    out["claims"] = self.db.replace_claims(
                        source_id, [c.to_row() for c in claims]
                    )
                for w in warns:
                    self.progress(f"  {w}")
            elif cfg.extract_claims:
                self.progress(
                    f"密度 {digest.density_score} 低于 {cfg.claim_min_density}，跳过 claim 抽取"
                )

            # ---- 记录未解疑问（供后续内容回答）----
            for question in (digest.open_questions or [])[:3]:
                if question and not question.startswith(("作者承认", "需警惕", "原文包含")):
                    self.db.add_question(question, source_id=source_id)

            # ---- 三级漏斗关联 ----
            retriever = None
            try:
                from .search import Retriever

                retriever = Retriever(self.db, self.config.retrieval)
            except Exception:  # noqa: BLE001 - 无向量能力时降级为规则关联
                retriever = None

            linker = Linker(
                self.db,
                router=router,
                retriever=retriever,
                progress=self.progress,
                min_confidence=cfg.min_relation_confidence,
                max_relations=cfg.max_relations_per_source,
            )
            result = linker.link(source_id, digest=digest)
            out["findings"] = format_findings(result)
            for note in result.notes:
                self.progress(f"  {note}")

        except Exception as exc:  # noqa: BLE001
            # 只打 message 会让偶发问题无从下手（实测遇到过一次
            # FOREIGN KEY constraint failed，重跑无法复现且看不到出错位置）。
            # 设 DISTILL_DEBUG=1 时打完整堆栈。
            self.progress(f"知识关联失败（不影响浓缩结果）：{exc}")
            if os.environ.get("DISTILL_DEBUG"):
                import traceback

                self.progress(traceback.format_exc())

        return out

    def _try_index(self, source_id: int, raw: RawContent) -> int:
        if not self.config.retrieval.auto_index:
            return 0
        try:
            from .search import Retriever

            retriever = Retriever(
                self.db, self.config.retrieval, progress=self.progress
            )
            n = retriever.index_source(source_id, raw.segments)
            if n:
                self.progress(f"已建检索索引（{n} 块），可用 distill ask 提问")
            return n
        except Exception as exc:  # noqa: BLE001 - 索引失败不应让消化白做
            self.progress(f"建索引失败（不影响浓缩结果）：{exc}")
            return 0

    def add_url(self, url: str, *, force: bool = False) -> IngestResult:
        return self.process(self.fetch(url), force=force)

    # ---------- 导出 ----------

    def _distill_paper(self, raw: RawContent, router: LLMRouter) -> Digest:
        """论文浓缩：用论文专属 schema，结果映射到通用 Digest 以复用存储与展示。"""
        from .paper import PaperSummarizer
        from .paper.clients import PaperMeta

        extra = raw.extra or {}
        meta = PaperMeta(
            title=raw.title,
            authors=(raw.author or "").split("; ") if raw.author else [],
            year=extra.get("year"),
            venue=extra.get("venue"),
            doi=extra.get("doi"),
            arxiv_id=extra.get("arxiv_id"),
            citation_count=extra.get("citation_count"),
            is_preprint=extra.get("is_preprint"),
        )

        summarizer = PaperSummarizer(self.config, router, progress=self.progress)
        payload = summarizer.run(meta, raw.segments)

        from .distill.density import score_density

        density = score_density("\n".join(s.text for s in raw.segments))
        return _paper_payload_to_digest(payload, density)

    def raw_from_db(self, source: Source, segments: list) -> RawContent:
        """从库内记录重建 RawContent。用于补建关联时复用同一套逻辑。"""
        row = self.db.conn.execute(
            "SELECT extra_json FROM sources WHERE id = ?", (source.id,)
        ).fetchone()
        extra = {}
        if row:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except json.JSONDecodeError:
                extra = {}
        return RawContent(
            url=source.url,
            content_type=source.content_type,
            platform=source.platform,
            title=source.title,
            author=source.author,
            publish_at=source.publish_at,
            duration_sec=source.duration_sec,
            lang=source.lang,
            segments=segments,
            extra=extra,
        )

    # ---------- 导出 ----------

    def _write_markdown(self, source: Source, digest: Digest) -> str:
        md = render_digest(source, digest)
        stem = safe_filename(f"{source.id:04d}-{source.title}")
        path = self.paths.digests / f"{stem}.md"
        write_text(path, md)
        return self.paths.relative(path)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Pipeline", "IngestResult", "FetchError"]


def _paper_payload_to_digest(payload: dict, density) -> Digest:
    """论文 payload → 通用 Digest。

    这样论文能复用 show / export / 索引等已有能力，
    同时把论文特有字段（gap / 实验指标 / 局限）保留在结构化位置。
    """
    from .models import Concept, OutlineItem

    method = payload.get("method") or {}
    experiments = payload.get("experiments") or {}
    repro = payload.get("reproducibility") or {}

    # TL;DR：问题 + 创新点 + 主要结论
    tldr: list[str] = []
    if payload.get("problem"):
        tldr.append(f"问题：{payload['problem']}")
    if method.get("core_idea"):
        tldr.append(f"方法：{method['core_idea']}")
    tldr.extend(payload.get("conclusion") or [])
    if payload.get("takeaway"):
        tldr.append(f"价值：{payload['takeaway']}")

    outline: list[OutlineItem] = []
    if payload.get("gap"):
        outline.append(
            OutlineItem(title="前人不足（立论基础）", points=[payload["gap"]], anchor="introduction")
        )
    if method.get("detail"):
        points = list(method["detail"])
        if method.get("novelty"):
            points.append(f"本质区别：{method['novelty']}")
        outline.append(OutlineItem(title="方法细节", points=points, anchor="method"))

    exp_points: list[str] = []
    if experiments.get("datasets"):
        exp_points.append(f"数据集：{'、'.join(experiments['datasets'])}")
    if experiments.get("baselines"):
        exp_points.append(f"基线：{'、'.join(experiments['baselines'])}")
    for m in experiments.get("metrics") or []:
        vs = f"（{m['vs_baseline']}）" if m.get("vs_baseline") else ""
        exp_points.append(f"{m.get('name')}：{m.get('value')}{vs}")
    if experiments.get("ablation"):
        exp_points.append(f"消融：{experiments['ablation']}")
    if exp_points:
        outline.append(OutlineItem(title="实验与结果", points=exp_points, anchor="experiment"))

    repro_bits: list[str] = []
    if repro.get("code_url"):
        repro_bits.append(f"代码：{repro['code_url']}")
    repro_bits.append("数据可获取" if repro.get("data_available") else "数据未公开")
    if repro.get("difficulty"):
        repro_bits.append(f"复现难度：{repro['difficulty']}")
    if repro_bits:
        outline.append(OutlineItem(title="可复现性", points=repro_bits))

    cred = payload.get("credibility") or {}
    concepts: list[Concept] = []
    if cred:
        parts = []
        if cred.get("venue"):
            parts.append(str(cred["venue"]))
        if cred.get("citations") is not None:
            parts.append(f"被引 {cred['citations']}")
        # 用三态状态描述，避免把「未确认」显示成「预印本」
        parts.append(str(cred.get("status") or "发表状态未知"))
        concepts.append(Concept(term="可信度", definition=" · ".join(parts)))

    return Digest(
        prompt_version=payload.get("prompt_version", "paper-v1"),
        tldr=tldr,
        outline=outline,
        concepts=concepts,
        # 作者承认的局限 + 我方观察到的问题分开呈现
        open_questions=(
            [f"作者承认：{x}" for x in (payload.get("limitations") or [])]
            + [f"需警惕：{x}" for x in (payload.get("my_concerns") or [])]
        ),
        contrarian=[],
        actions=[],
        density_score=density.score,
        density_verdict=density.verdict,
        model_used=payload.get("model_used"),
        tokens_in=payload.get("tokens_in", 0),
        tokens_out=payload.get("tokens_out", 0),
        cost_usd=payload.get("cost_usd", 0.0),
    )

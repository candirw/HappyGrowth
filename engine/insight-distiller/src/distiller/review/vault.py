"""Obsidian 双链导出。

**为什么值得做**：库内的关系与实体已经是图结构，
导出成 wikilink 后可直接用 Obsidian 的图谱视图浏览——
不必自己写可视化，也让知识能脱离本工具存在（避免数据绑定）。

三类文件：
1. 内容笔记 —— frontmatter + 双链到关联内容与概念
2. 概念页 —— 反向索引：该概念出现在哪些内容里
3. MOC（内容地图）—— 按领域/类型组织的入口页

文件名必须过 safe_filename：Obsidian 库常在 Windows 上打开，
非法字符会导致文件无法创建。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..compat.fsio import write_text
from ..compat.paths import safe_filename
from ..models import Digest, Source
from ..store import Database
from ..knowledge.linker import REL_LABELS

# Obsidian wikilink 里这些字符会破坏链接语法
_LINK_UNSAFE = re.compile(r"[\[\]|#^]")


def wikilink_title(title: str, *, source_id: int | None = None) -> str:
    """生成 wikilink 用的标题。

    带 id 前缀避免同名内容冲突——同一博主的系列视频常有重复标题。
    """
    cleaned = _LINK_UNSAFE.sub("", title or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:70]
    if not cleaned:
        cleaned = f"内容{source_id or ''}"
    if source_id is not None:
        return f"{source_id:04d} {cleaned}"
    return cleaned


def entity_page_name(name: str) -> str:
    return _LINK_UNSAFE.sub("", name or "").strip()[:60] or "未命名概念"


@dataclass
class VaultStats:
    notes: int = 0
    entities: int = 0
    mocs: int = 0
    skipped: list[str] = field(default_factory=list)


def render_note(
    source: Source,
    digest: Digest,
    *,
    relations: list[dict],
    entities: list[str],
    notes: list[dict],
    domains: list[str],
) -> str:
    """渲染带双链的内容笔记。"""
    lines: list[str] = ["---"]
    lines.append(f'title: "{(source.title or "").replace(chr(34), chr(39))}"')
    lines.append(f"url: {source.url}")
    lines.append(f"platform: {source.platform}")
    lines.append(f"type: {source.content_type.value}")
    lines.append(f"source_id: {source.id}")
    if source.author:
        lines.append(f'author: "{source.author}"')
    if source.publish_at:
        lines.append(f"publish_at: {source.publish_at.date()}")
    if digest.density_score is not None:
        lines.append(f"density: {digest.density_score}")

    # tags 让 Obsidian 能按领域/类型筛选
    tags = ["insight-distiller", source.content_type.value]
    tags.extend(domains)
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append("---")
    lines.append("")

    lines.append(f"# {source.title}")
    lines.append("")

    meta = [f"[原文]({source.url})"]
    if source.author:
        meta.append(f"作者：{source.author}")
    if source.duration_sec:
        meta.append(f"时长：{source.duration_sec // 60} 分钟")
    lines.append(" · ".join(meta))
    lines.append("")

    if digest.density_score is not None:
        lines.append(
            f"> **信息密度 {digest.density_score}/100** —— {digest.density_verdict}"
        )
        lines.append("")

    # 关系放在最前面：打开笔记第一眼就看到它在知识网络中的位置
    if relations:
        lines.append("## 关联")
        lines.append("")
        for rel in relations:
            icon, label = REL_LABELS.get(rel["rel_type"], ("·", rel["rel_type"]))
            link = wikilink_title(rel["title"], source_id=rel["id"])
            line = f"- {icon} **{label}** [[{link}]]"
            if rel.get("note"):
                line += f" —— {rel['note']}"
            lines.append(line)
            # 冲突附双方主张，让人在 Obsidian 里也能直接判断
            if rel["rel_type"] == "contradicts" and rel.get("new_claim"):
                lines.append(f"    - 本文：{rel['new_claim']}")
                lines.append(f"    - 对方：{rel['old_claim']}")
        lines.append("")

    def section(heading: str, items: list[str]) -> None:
        if not items:
            return
        lines.append(f"## {heading}")
        lines.append("")
        lines.extend(f"- {item}" for item in items)
        lines.append("")

    section("核心结论", list(digest.tldr or []))
    section("反直觉观点", list(digest.contrarian or []))

    if digest.outline:
        lines.append("## 分层大纲")
        lines.append("")
        for item in digest.outline:
            anchor = f" `{item.anchor}`" if item.anchor else ""
            lines.append(f"### {item.title}{anchor}")
            lines.append("")
            lines.extend(f"- {p}" for p in item.points)
            lines.append("")

    # 概念用双链，这样 Obsidian 图谱能连起来
    if digest.concepts:
        lines.append("## 概念与术语")
        lines.append("")
        for c in digest.concepts:
            page = entity_page_name(c.term)
            lines.append(f"- [[{page}]]：{c.definition}")
        lines.append("")

    extra_entities = [e for e in entities if e not in {c.term for c in digest.concepts}]
    if extra_entities:
        lines.append("## 涉及概念")
        lines.append("")
        lines.append(
            " · ".join(f"[[{entity_page_name(e)}]]" for e in extra_entities[:20])
        )
        lines.append("")

    if digest.quotes:
        lines.append("## 金句")
        lines.append("")
        for q in digest.quotes:
            anchor = f" — `{q.anchor}`" if q.anchor else ""
            lines.append(f"> {q.text}{anchor}")
            lines.append("")

    if digest.actions:
        lines.append("## 可执行项")
        lines.append("")
        # 用 checkbox，Obsidian 里可直接勾选
        lines.extend(f"- [ ] {a}" for a in digest.actions)
        lines.append("")

    section("存疑与未解", list(digest.open_questions or []))

    # 我的批注放最后但保留——这是自己的思考，比 AI 摘要更值钱
    if notes:
        lines.append("## 我的批注")
        lines.append("")
        for n in notes:
            lines.append(f"- **[{n['note_type']}]** {n['text']}")
        lines.append("")

    lines.append("---")
    lines.append("")
    cost = f"${digest.cost_usd:.4f}" if digest.cost_usd else "$0"
    lines.append(
        f"*由 {digest.model_used or '未知模型'} 生成 · "
        f"tokens {digest.tokens_in}/{digest.tokens_out} · 成本 {cost}*"
    )
    return "\n".join(lines)


def render_entity_page(
    name: str,
    *,
    etype: str | None,
    definition: str | None,
    aliases: list[str],
    sources: list[dict],
) -> str:
    """概念页：反向索引哪些内容提到了它。

    这是 Obsidian 双链的核心价值——从概念出发找回所有相关内容。
    """
    lines = ["---"]
    lines.append(f'title: "{entity_page_name(name)}"')
    if etype:
        lines.append(f"entity_type: {etype}")
    if aliases:
        lines.append(f"aliases: [{', '.join(aliases[:8])}]")
    lines.append("tags: [insight-distiller/concept]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")

    if definition:
        lines.append(f"> {definition}")
        lines.append("")

    if aliases:
        lines.append(f"**别名**：{'、'.join(aliases)}")
        lines.append("")

    lines.append(f"## 出现在 {len(sources)} 篇内容中")
    lines.append("")
    for s in sources:
        link = wikilink_title(s["title"], source_id=s["id"])
        ctype = f"`{s['type']}`" if s.get("type") else ""
        lines.append(f"- [[{link}]] {ctype}")
        if s.get("context"):
            lines.append(f"  - {s['context'][:120]}")
    lines.append("")
    return "\n".join(lines)


def render_moc(
    *,
    by_domain: dict[str, list[dict]],
    by_type: dict[str, list[dict]],
    hot_entities: list[dict],
    total: int,
) -> str:
    """内容地图（MOC）：Obsidian 库的入口页。"""
    lines = ["---", 'title: "知识库总览"', "tags: [insight-distiller/moc]", "---", ""]
    lines.append("# 知识库总览")
    lines.append("")
    lines.append(f"共 **{total}** 篇内容。")
    lines.append("")

    if hot_entities:
        lines.append("## 高频概念")
        lines.append("")
        lines.append(
            " · ".join(
                f"[[{entity_page_name(e['name'])}]] ({e['count']})"
                for e in hot_entities[:20]
            )
        )
        lines.append("")

    if by_domain:
        lines.append("## 按领域")
        lines.append("")
        for domain, items in sorted(by_domain.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"### {domain}（{len(items)}）")
            lines.append("")
            for s in sorted(items, key=lambda x: -(x.get("density") or 0))[:25]:
                link = wikilink_title(s["title"], source_id=s["id"])
                score = f" · 密度 {s['density']:.0f}" if s.get("density") else ""
                lines.append(f"- [[{link}]]{score}")
            lines.append("")

    lines.append("## 按类型")
    lines.append("")
    for ctype, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {ctype}（{len(items)}）")
        lines.append("")
        for s in sorted(items, key=lambda x: -(x.get("density") or 0))[:25]:
            link = wikilink_title(s["title"], source_id=s["id"])
            lines.append(f"- [[{link}]]")
        lines.append("")

    return "\n".join(lines)


class VaultExporter:
    def __init__(self, db: Database, *, min_entity_mentions: int = 2) -> None:
        self.db = db
        # 只导出跨内容出现的概念——单次提及的概念页没有连接价值
        self.min_entity_mentions = min_entity_mentions

    def export(
        self,
        out_dir: Path,
        *,
        include_entities: bool = True,
        include_moc: bool = True,
        limit: int | None = None,
    ) -> VaultStats:
        out_dir = Path(out_dir).expanduser()
        notes_dir = out_dir / "notes"
        concepts_dir = out_dir / "concepts"
        notes_dir.mkdir(parents=True, exist_ok=True)
        if include_entities:
            concepts_dir.mkdir(parents=True, exist_ok=True)

        stats = VaultStats()
        by_domain: dict[str, list[dict]] = {}
        by_type: dict[str, list[dict]] = {}

        sources = self.db.list_sources(limit=limit or 10_000)
        for src in sources:
            if src.id is None:
                continue
            digest = self.db.get_latest_digest(src.id)
            if digest is None:
                stats.skipped.append(f"#{src.id} 尚未浓缩")
                continue

            relations = self._relations(src.id)
            entities = self._entities_of(src.id)
            notes = [
                {"text": n["text"], "note_type": n["note_type"]}
                for n in self.db.get_notes(src.id)
            ]
            domains = self._domains(src.id)

            content = render_note(
                src, digest,
                relations=relations,
                entities=entities,
                notes=notes,
                domains=domains,
            )
            fname = safe_filename(wikilink_title(src.title, source_id=src.id))
            write_text(notes_dir / f"{fname}.md", content)
            stats.notes += 1

            entry = {
                "id": src.id,
                "title": src.title,
                "density": digest.density_score,
                "type": src.content_type.value,
            }
            by_type.setdefault(src.content_type.value, []).append(entry)
            for d in domains or ["未分类"]:
                by_domain.setdefault(d, []).append(entry)

        hot_entities: list[dict] = []
        if include_entities:
            for row in self.db.list_entities(
                min_mentions=self.min_entity_mentions, limit=500
            ):
                sources_of = [
                    {
                        "id": int(s["id"]),
                        "title": s["title"],
                        "type": s["content_type"],
                        "context": s["context"],
                    }
                    for s in self.db.entity_sources(int(row["id"]))
                ]
                if not sources_of:
                    continue
                try:
                    aliases = json.loads(row["aliases_json"] or "[]")
                except json.JSONDecodeError:
                    aliases = []
                page = render_entity_page(
                    row["name"],
                    etype=row["type"],
                    definition=row["definition"],
                    aliases=aliases,
                    sources=sources_of,
                )
                fname = safe_filename(entity_page_name(row["name"]))
                write_text(concepts_dir / f"{fname}.md", page)
                stats.entities += 1
                hot_entities.append(
                    {"name": row["name"], "count": int(row["mention_count"])}
                )

        if include_moc:
            moc = render_moc(
                by_domain=by_domain,
                by_type=by_type,
                hot_entities=hot_entities,
                total=stats.notes,
            )
            write_text(out_dir / "知识库总览.md", moc)
            stats.mocs = 1

        return stats

    # ---------- 辅助 ----------

    def _relations(self, source_id: int) -> list[dict]:
        out: list[dict] = []
        for row in self.db.get_relations(source_id, limit=20):
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            out.append(
                {
                    "id": int(row["other_id"]),
                    "title": row["other_title"],
                    "rel_type": row["rel_type"],
                    "note": evidence.get("note"),
                    "new_claim": evidence.get("new_claim"),
                    "old_claim": evidence.get("old_claim"),
                }
            )
        return out

    def _entities_of(self, source_id: int) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT e.name FROM entity_mentions m JOIN entities e ON e.id = m.entity_id "
            "WHERE m.source_id = ? AND e.mention_count >= ? ORDER BY e.mention_count DESC",
            (source_id, self.min_entity_mentions),
        ).fetchall()
        return [r["name"] for r in rows]

    def _domains(self, source_id: int) -> list[str]:
        row = self.db.conn.execute(
            "SELECT domains_json FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["domains_json"] or "[]")
        except json.JSONDecodeError:
            return []

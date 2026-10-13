"""Markdown 渲染。

导出为 Obsidian 友好格式：frontmatter + 双链预留。
文件名必须过 safe_filename（Windows 非法字符与保留名）。
"""

from __future__ import annotations

from ..models import Digest, Source


def render_digest(source: Source, digest: Digest, *, frontmatter: bool = True) -> str:
    lines: list[str] = []

    if frontmatter:
        lines += [
            "---",
            f'title: "{source.title.replace(chr(34), chr(39))}"',
            f"url: {source.url}",
            f"platform: {source.platform}",
            f"type: {source.content_type.value}",
        ]
        if source.author:
            lines.append(f'author: "{source.author}"')
        if source.publish_at:
            lines.append(f"publish_at: {source.publish_at.date()}")
        if digest.density_score is not None:
            lines.append(f"density: {digest.density_score}")
        lines += [f"model: {digest.model_used or ''}", "---", ""]

    lines.append(f"# {source.title}")
    lines.append("")

    meta_bits = [f"[原文]({source.url})"]
    if source.author:
        meta_bits.append(f"作者：{source.author}")
    if source.duration_sec:
        meta_bits.append(f"时长：{source.duration_sec // 60} 分钟")
    lines.append(" · ".join(meta_bits))
    lines.append("")

    if digest.density_score is not None:
        lines.append(
            f"> **信息密度 {digest.density_score}/100** —— {digest.density_verdict}"
        )
        lines.append("")

    def section(heading: str, items: list[str]) -> None:
        if not items:
            return
        lines.append(f"## {heading}")
        lines.append("")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")

    section("核心结论", digest.tldr)

    if digest.contrarian:
        section("反直觉观点", digest.contrarian)

    if digest.outline:
        lines.append("## 分层大纲")
        lines.append("")
        for item in digest.outline:
            anchor = f" `{item.anchor}`" if item.anchor else ""
            lines.append(f"### {item.title}{anchor}")
            lines.append("")
            for point in item.points:
                lines.append(f"- {point}")
            lines.append("")

    if digest.concepts:
        lines.append("## 概念与术语")
        lines.append("")
        for c in digest.concepts:
            lines.append(f"- **{c.term}**：{c.definition}")
        lines.append("")

    if digest.quotes:
        lines.append("## 金句")
        lines.append("")
        for q in digest.quotes:
            anchor = f" — `{q.anchor}`" if q.anchor else ""
            lines.append(f"> {q.text}{anchor}")
            lines.append("")

    section("可执行项", digest.actions)
    section("存疑与未解", digest.open_questions)

    lines.append("---")
    lines.append("")
    cost = f"${digest.cost_usd:.4f}" if digest.cost_usd else "$0"
    lines.append(
        f"*由 {digest.model_used or '未知模型'} 生成 · "
        f"tokens {digest.tokens_in}/{digest.tokens_out} · 成本 {cost}*"
    )

    return "\n".join(lines)

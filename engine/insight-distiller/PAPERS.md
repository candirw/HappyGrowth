# 学术论文模块设计

> 输入：arXiv 链接 / DOI / 本地 PDF / 论文标题
> 输出：结构化精读笔记 + **相似论文推荐** + 与你已读论文的关系判定

---

## 1. 为什么论文不能复用视频那套链路

| 维度 | 视频 / 博客 | 论文 |
|------|------------|------|
| 结构 | 线性叙述 | 固定章节（Abstract/Method/Experiment/Limitation） |
| 关键信息 | 观点、金句 | **方法、实验设置、指标数字、局限** |
| 相似性判断 | 语义相近即可 | **引文关系是更强信号**（共被引、文献耦合） |
| 外部元数据 | 几乎没有 | 有完整学术图谱（引用数、参考文献、被引） |
| 浓缩重点 | "值不值得看" | "**和我读过的那几篇什么关系**" |

结论：论文需要**独立的解析器 + 独立的浓缩 schema + 专属的相似检索链路**。
因此 `sources` 表从 M1 起就要有 `content_type` 字段做分流，避免后期改数据模型。

---

## 2. 数据源能力（已实测确认，2026-09）

| 源 | 能力 | 限流现实 | 定位 |
|----|------|---------|------|
| **Semantic Scholar** | **有专门的 Recommendations 接口**（给定论文直接返回相似论文）；2 亿+ 论文；提供引用上下文 | 无 key：1000 req/s **但所有匿名用户共享**（实际频繁 429）；有 key：入门仅 **1 req/s** | 相似推荐主力，**必须做缓存 + 指数退避** |
| **OpenAlex** | 2.5 亿+ 作品，无需 API key；有 `related_works` 字段、完整引用网络、概念标签 | 积分制：免费**每日 10 万积分**（列表请求 10 分 → 约 1 万次列表查询/天），每秒 100 次；加 `mailto=` 进 polite pool 更稳 | 元数据与引文图谱主力，额度充裕 |
| **arXiv** | 官方 API + PDF 全文，预印本最新 | 建议 3 秒/请求 | 全文获取 |
| **Crossref** | DOI 解析、期刊元数据 | 加 mailto 即可 | DOI 兜底 |

**关键工程约束**：
- S2 有 key 反而只有 1 req/s，所以**元数据批量拉取走 OpenAlex，相似推荐才用 S2**，且结果永久缓存（论文的引文关系变化很慢，缓存 30 天完全够）。
- OpenAlex 支持 **OR 语法一次合并最多 50 个 ID**，批量补元数据时请求数直接降到 1/50，这是省额度的关键。

---

## 3. 论文解析

```
输入 → 识别类型（arXiv ID / DOI / PDF / 标题）
  ↓
元数据对齐：OpenAlex + S2 → 拿到 paper_id / 引用数 / 参考文献 / 被引列表
  ↓
全文获取：arXiv PDF 或本地 PDF
  ↓
结构化抽取：pymupdf4llm（快，够用）→ 复杂排版降级 GROBID（准，Docker 部署）
  ↓
分节存储：abstract / intro / method / experiment / conclusion / limitation / references
```

论文 PDF 的三个实际难点与处理：
- **双栏排版**乱序 → `pymupdf4llm` 已处理，失败则用 GROBID。
- **公式** → 保留 LaTeX 或转 `[FORMULA]` 占位，不强求识别（对理解贡献有限，成本极高）。
- **图表** → 抽 caption 即可，图片本身存路径备查；`Table` 里的指标数字尽量抽成结构化（用于后续对比表格）。

**分节抽取的价值**：浓缩时可以只把 Method + Experiment 送给 LLM，token 省一半且结论更准。

---

## 4. 论文专属浓缩 schema

```jsonc
{
  "problem": "要解决什么问题",
  "gap": "前人方法的不足（论文的立论基础）",
  "method": { "core_idea": "一句话说清创新点", "detail": ["…"], "novelty": "…" },
  "experiments": {
    "datasets": ["…"], "baselines": ["…"],
    "metrics": [ { "name": "…", "value": "…", "vs_baseline": "+3.2%" } ],
    "ablation": "消融实验说明了什么"
  },
  "conclusion": ["…"],
  "limitations": ["作者承认的 + 你该警惕的"],
  "my_takeaway": "对我有什么用（结合你的知识库上下文生成）",
  "reproducibility": { "code_url": "…", "data_available": true, "difficulty": "中" },
  "credibility": {                       // 论文特有：可信度而非信息密度
    "venue": "NeurIPS 2025", "citations": 142,
    "is_preprint": false, "peer_reviewed": true
  }
}
```

`limitations` 和 `gap` 是多数 AI 摘要会忽略但价值最高的两项——它们直接告诉你这方法能不能用在你的场景。

---

## 5. 相似论文检索：四路召回融合

单一策略都有盲区，所以多路召回 + RRF 融合排序（与问答层的混合检索思路一致）：

| 路径 | 原理 | 擅长 | 盲区 |
|------|------|------|------|
| **① S2 Recommendations** | 基于用户共读行为的协同过滤 | 最接近"同行会推荐什么" | 冷门/极新论文无数据 |
| **② 引文图谱** | 参考文献 + 被引 + **共被引**（co-citation）+ **文献耦合**（bibliographic coupling） | 学术脉络精准，可靠性最高 | 新论文引用少 |
| **③ 语义向量** | **SPECTER2**（专为论文训练的 embedding，远优于通用模型）算标题+摘要相似度 | 覆盖新论文、跨领域发现 | 会召回"表面像但方法无关" |
| **④ 关键词 / 作者 / 概念** | OpenAlex concepts + 同作者后续工作 | 追踪某个团队的演进 | 召回宽泛 |

**为什么必须有②**：共被引与文献耦合是几十年验证的学术相似度指标——两篇论文被同一批文章同时引用，说明它们在解决同类问题，这个信号比向量相似度可靠得多。

融合后输出分组，而不是一个平铺列表：

```
$ distill paper similar 42

【奠基工作】被本文引用且高被引 —— 先补这些
  1. Attention Is All You Need (2017) · 引用 12.8万 · 已在你库中 #7 ✓
【同期竞品】文献耦合度高，解决同一问题的不同路线
  2. …  · 引用 890 · 与你库中 #23 方法冲突 ⚠
【后续进展】引用了本文，可能已超越它
  3. …  (2026) · 预印本 · 未读
【跨领域启发】语义相近但引文无交集 ← 最容易漏掉的价值点
  4. …
```

「跨领域启发」这一组只有靠语义向量才能发现，也是最容易产生新想法的地方。

---

## 6. 论文相关命令

```bash
# 精读一篇（自动补元数据 + 结构化笔记）
distill add "https://arxiv.org/abs/2501.xxxxx"
distill add ~/Downloads/paper.pdf
distill add --doi 10.1145/xxxxx

# 找相似（四路召回融合，分组展示）
distill paper similar 42
distill paper similar 42 --strategy citation   # 只走引文图谱
distill paper similar 42 --unread              # 只看没读过的
distill paper similar 42 --add 1,3             # 直接把第1、3篇加入消化队列

# 追根溯源：自动构建阅读路径（拓扑排序，先读奠基工作）
distill paper lineage 42

# 多篇横向对比 → 生成方法/数据集/指标对比表
distill paper compare 42 43 51

# 从一篇论文出发做小型综述（自动扩展 N 篇再汇总）
distill paper survey 42 --depth 2 --limit 15
```

`lineage`（阅读路径）解决的是"论文看不懂因为缺前置知识"——按引文依赖拓扑排序，告诉你该先读哪篇。

---

## 7. 风险与应对

| 风险 | 应对 |
|------|------|
| S2 匿名限额被别人占满、有 key 也只 1 req/s | 本地缓存 30 天 + 指数退避 + 降级到 OpenAlex `related_works` |
| OpenAlex 每日积分耗尽 | OR 语法合并 50 ID/请求；缓存优先；加 `mailto` 进 polite pool |
| PDF 解析乱序导致摘要错 | `pymupdf4llm` → GROBID 降级；抽取后做完整性校验（章节缺失则告警） |
| 推荐结果泛滥（几百篇） | 默认只出 Top10 分组展示，且标注"已在库中"避免重复消化 |
| 预印本质量参差 | `credibility` 字段显式标注 is_preprint / 引用数 / 会议，不把预印本与顶会论文等同呈现 |
| 中文论文（知网/万方）覆盖差 | 明确告知这是硬限制；支持本地 PDF 手动导入作为兜底 |

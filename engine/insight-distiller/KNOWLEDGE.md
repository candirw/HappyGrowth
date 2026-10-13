# 知识管理层设计

> 解决两件事：
> ① **新内容自动关联历史** —— 你丢进来的 URL / 论文，如果和以前看过的有关系，工具主动告诉你
> ② **重温机制** —— 不是"存下就忘"，而是让沉淀的知识能被再次找到、再次唤起

---

## 1. 核心理念：入库那一刻就建立连接

传统笔记工具的问题是「**写入即遗忘**」——存了 500 篇，实际再也没打开过。
根因是**关联要靠人手动建立**（打标签、做双链），成本高到坚持不下来。

本方案的做法：**消化流程的最后一步强制跑一次关联检测**，产出如下即时反馈：

```
$ distill add "https://arxiv.org/abs/2501.xxxxx"
✓ 已消化：《…》
✓ 信息密度 78 · 值得读原文

⚡ 与你的知识库发现 4 处关联：
  ├─ 🔗 引用了你读过的 #7《Attention Is All You Need》
  ├─ ⚠️ 结论与 #23《…》冲突：本文认为 RAG 召回应重排，#23 主张端到端微调
  ├─ 📈 是 #31 的后续工作（同作者，方法迭代）
  └─ 🎯 可回答你 3 周前在 #18 留下的未解问题："长文档如何分块？"

💡 建议：先重温 #23 的第 4 节再读本文，两者分歧点在评测集选择上
```

最后两条是这套系统真正的价值：**它记得你的疑问，并在答案出现时提醒你**。

---

## 2. 数据模型（M1 就要定好，避免后期迁移）

```sql
-- 内容主表：视频/博客/论文统一存，靠 content_type 分流
sources(id, content_type, platform, url, title, author, publish_at,
        duration, lang, status, density_score, credibility,
        domains_json, authority, created_at)

-- 转写/正文片段（带时间戳或章节锚点）
segments(id, source_id, idx, start_ms, end_ms, section, text)

-- 向量检索单元
chunks(id, source_id, segment_ids, text, embedding_id, token_count)

-- 浓缩结果（schema 随 content_type 变化，存 JSON）
digests(id, source_id, schema_version, payload_json, model, cost, created_at)

-- ⭐ 实体：概念/术语/人物/工具/数据集，跨内容归一
entities(id, name, type, aliases_json, definition, embedding_id, mention_count)
entity_mentions(entity_id, source_id, segment_id, context)

-- ⭐ 原子知识单元：给 agent 消费（详见 AGENT_API.md）
claims(id, source_id, segment_ids_json, domain, subject, predicate,
       claim_type, polarity, confidence, as_of, expires_hint,
       numeric_json, conditions_json, authority, contradicts_json)

-- ⭐ 关系：知识图谱的边
relations(id, from_source, to_source, rel_type, confidence,
          evidence_json, direction, created_at, verified_by_user)

-- ⭐ 我的疑问追踪
questions(id, source_id, text, status, answered_by_source, created_at)

-- ⭐ 重温调度（间隔重复）
reviews(source_id, ease, interval_days, due_at, review_count, last_result)

-- 我的批注（区分「作者说的」与「我想的」）
notes(id, source_id, segment_id, text, note_type, created_at)

-- 问答历史
messages(id, session_id, source_ids_json, role, content, citations_json)
```

**为什么不用 Neo4j**：关系规模在个人使用场景下最多几万条边，SQLite 递归 CTE 做 2–3 跳遍历完全够用，且零运维、单文件可备份。引入图数据库是过度设计。

---

## 3. 关系类型（有区分度才有价值）

只做「相关」是没用的——必须区分**是什么关系**：

| 类型 | 含义 | 判定方式 | 价值 |
|------|------|---------|------|
| `cites` | 论文引用 | 引文 API，**确定性，零成本** | 学术脉络 |
| `same_topic` | 同主题 | 向量相似度 > 阈值 | 归类 |
| **`contradicts`** | **结论冲突** | LLM 对比核心结论 | **最高**——认知升级往往来自发现矛盾 |
| `supports` | 相互印证 | LLM 判定 | 增强置信 |
| `extends` | 后续/迭代 | 引文 + 时间 + 同作者 | 追踪演进 |
| `prerequisite` | 前置知识 | 概念依赖 + 引文拓扑 | 生成阅读路径 |
| `answers` | 回答了历史疑问 | 疑问向量 vs 新内容检索 | 闭环感极强 |
| `duplicate` | 内容重复 | 高相似 + 同主题 | **避免重复消化，省钱** |

`contradicts` 的实现要谨慎：**只在双方都有明确结论句时判定**，并附上双方原文片段作为证据（`evidence_json`），让你自己判断谁对，而不是让 LLM 裁决。

---

## 4. 自动关联的成本控制

新内容入库要和历史库比对，全量跑 LLM 会失控。三级漏斗：

```
新内容入库
  ↓
① 免费层：引文关系（论文有 API）+ 实体共现（SQL join，命中即确定关系）
  ↓
② 向量层：用 digest 的 TL;DR 做 embedding，检索历史库 Top 20 候选（本地，零成本）
  ↓
③ LLM 层：仅对 Top 5 候选做关系判定，一次批量调用，输出 rel_type + confidence + 证据
```

**成本**：每篇新内容额外约 1 次 LLM 调用（~2k token，DeepSeek 约 ¥0.003），可忽略。
即使库里有 1000 篇，成本也不随库规模增长——这是关键设计点。

`relations` 表的 `verified_by_user` 字段：你可以确认或否决 LLM 判定的关系，被否决的模式会写入负样本，同类误判后续降权。

---

## 5. 实体归一：让「同一个概念」真正合并

同一概念在不同内容里写法不同（`RAG` / 检索增强生成 / Retrieval-Augmented Generation），不归一就无法关联。

策略（渐进，不追求完美）：
1. 抽取阶段让 LLM 输出规范名 + 原文表述。
2. 用 embedding 相似度 + 编辑距离做候选合并，高置信自动合并。
3. 边缘案例进「待确认」队列，`distill entity merge` 一条命令批量处理。
4. 用户可维护 `aliases.yaml` 手工纠正，越用越准。

实体表的副产品是**个人术语词典**：`distill entity show RAG` 能看到这个概念在你库里被提到 27 次、分布在 9 篇内容、各自定义有何差异——这本身就是极高密度的知识产出。

---

## 6. 重温机制

三条互补路径，覆盖不同场景：

**① 主动检索**（想起来时找）
```bash
distill search "分块策略"              # 混合检索，跨全库
distill ask "我看过的内容里关于X有哪些不同做法？"   # 直接问，带出处
```

**② 间隔重复**（防遗忘）
```bash
distill review                    # 今日待重温，只推 TL;DR，30 秒一条
distill review --hard             # 只看标记为难/重要的
```
不做成 Anki 那种严格记忆卡（对成年人的技术学习负担过重），而是**轻量唤醒**：只展示 TL;DR + 你的批注，你点「记得 / 模糊」调整下次间隔（SM-2 简化版）。默认只对 `density_score > 60` 或你手动标星的内容排期，避免被水文淹没。

**③ 被动唤醒**（最有效）
- 新内容入库时自动提示相关旧内容（§1 的场景）。
- 每周简报：`distill digest --weekly` 汇总本周消化内容 + 主题聚类 + 你的未解疑问。
- 「陈旧提醒」：某主题你 6 个月没更新，但该领域出了新东西 → 主动提示（M4 订阅源支持后）。

**为什么被动唤醒最有效**：它不需要你「想起要复习」这个前提，而是在你自然使用工具时顺带完成。

---

## 7. 图谱与导出

```bash
distill graph                       # 全局主题聚类概览（终端树状 + 可导 HTML）
distill graph 42 --depth 2          # 以某篇为中心的关系网
distill timeline "RAG"              # 某主题的认知演进时间线
distill gaps                        # ⭐ 知识盲区：库中被反复引用但你从未消化的内容
```

`distill gaps` 是个高价值功能：统计所有内容里高频出现但你库中没有的概念/论文，直接告出你的知识缺口。

**Obsidian 双链导出**（配合你现有习惯）：
- 每篇一个 md，frontmatter 存元数据，关系导出为 `[[双链]]`。
- 实体导出为独立笔记页，自动形成 Obsidian 图谱视图。
- 双向：Obsidian 里手写的批注可回读入库（M4）。

---

## 8. 里程碑

| 阶段 | 内容 |
|------|------|
| **M1** | 只落 schema（含 `content_type`、`relations`、`entities` 表结构预留），不实现逻辑——**避免后期数据迁移** |
| **M3** | 论文模块（见 `PAPERS.md`）：引文关系是零成本的确定性关联，先把这条打通 |
| **M3.5** | 实体抽取 + 归一；三级漏斗自动关联；入库即提示（§1 场景）；`graph` / `gaps` |
| **M4** | 间隔重复重温；每周简报；Obsidian 双链导出；疑问追踪闭环 |

---

## 9. 风险与应对

| 风险 | 应对 |
|------|------|
| 关联提示噪音大，变成骚扰 | 只展示 confidence > 0.7 的关系，且每篇最多 5 条；`verified_by_user` 反馈降权误判 |
| LLM 误判「冲突」 | 必须附双方原文证据，只做提示不做裁决；无明确结论句则不判定 |
| 实体抽取产生大量垃圾实体 | 设 `mention_count >= 2` 才入图谱；停用词表过滤；定期 `entity prune` |
| 库大了检索变慢 | LanceDB 建索引；SQLite 加复合索引；关系遍历限制跳数 |
| 数据丢失 | 单文件 SQLite + 定期自动备份；所有内容可导出为纯 md（不锁定在本工具里） |
| 重温变成负担 | 默认只对高密度/标星内容排期；可随时 `--snooze`；不做强制打卡 |

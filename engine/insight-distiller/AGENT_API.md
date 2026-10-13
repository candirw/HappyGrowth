# Agent 友好设计

> 目标：知识库不只给你自己看，还要能被**外部 AI Agent**（如你后续要开发的理财 agent）当作知识后端调用。
> 核心判断：**给人看的散文摘要，和给 agent 用的知识，是两种不同的产物。** 只加个 API 并不等于 agent 友好。

---

## 1. 为什么"加个 API"不够

假设理财 agent 问："我该不该现在买长债？"
如果知识库只能返回一段摘要文本，agent 拿到的是：

> "UP主认为当前利率环境下长债配置价值凸显，建议逢低布局……"

这段东西对 agent 几乎是**有害**的：
- 不知道**这是谁说的**、可信度多少 → agent 会当成客观事实
- 不知道**什么时候说的** → 2024 年的利率判断放到 2026 年是错的
- 不知道**库里有没有相反观点** → agent 给出单边结论
- 无法**程序化提取**其中的数字与条件 → 没法参与计算
- 是"建议"而非"事实" → agent 可能直接转述成投资建议

所以 agent 友好的关键不是传输协议，而是**知识的形态**。要做四件事：

| # | 设计 | 解决什么 |
|---|------|---------|
| 1 | **原子知识单元 Claim** | 让 agent 能精确引用一条主张，而不是整篇文档 |
| 2 | **时效性与观点标注** | 区分事实/观点/预测，标注 `as_of` 与失效条件 |
| 3 | **主动暴露分歧** | 库里有冲突观点时，返回"存在分歧"而非单一答案 |
| 4 | **三种接入通道** | MCP（首选）/ HTTP API / Python SDK |

---

## 2. Claim：原子知识单元（本设计的核心）

在 `KNOWLEDGE.md` 的数据模型上增加一层。文档摘要是给人读的，Claim 是给 agent 用的。

```sql
claims(
  id,
  source_id,              -- 出自哪篇内容
  segment_ids_json,       -- 精确到原文哪几段，可溯源
  domain,                 -- ⭐ 领域分区：finance / tech / photography …
  subject,                -- 主题实体，如「长期国债」
  predicate,              -- 主张内容
  claim_type,             -- ⭐ fact | opinion | prediction | method | data
  polarity,               -- 支持 / 反对 / 中立
  confidence,             -- 抽取置信度
  as_of,                  -- ⭐ 该主张对应的时间点（不是入库时间）
  expires_hint,           -- ⭐ 预期失效时间或条件
  numeric_json,           -- ⭐ 结构化数字：{"利率":2.1,"单位":"%","期限":"10Y"}
  conditions_json,        -- 成立前提：{"假设":"降息周期持续"}
  authority,              -- 来源权威度：peer_reviewed|official|media|self_media|anonymous
  contradicts_json        -- 与库内哪些 claim 冲突
)
```

### 三个字段值得单独解释

**`claim_type`** —— 理财领域尤其致命。同一段内容里混着：
- `fact`：「2026 年 8 月 1 年期 LPR 为 3.0%」→ 可直接采信
- `opinion`：「我认为现在该加仓」→ 只能作为"某人的看法"
- `prediction`：「明年利率会下行」→ 未经验证，agent 必须标注不确定性
- `data`：可参与计算的历史数据

不区分这三者，agent 必然把自媒体观点当客观事实用。

**`as_of` + `expires_hint`** —— 金融知识会过期，这是与技术知识最大的区别。
「余额宝收益率 1.3%」这条 claim 在 2026 年可能已完全失效。所以：
- 检索时默认按 `as_of` 降权老旧 claim；
- agent 可显式传 `max_staleness_days` 过滤；
- 返回结果**强制携带 `staleness_days`**，让 agent 自己判断能不能用。

**`numeric_json`** —— 把文本里的数字抽成结构化，理财 agent 才能真正参与计算，而不是让 LLM 从散文里二次解析（极易出错）。

---

## 3. 领域分区（domain）

你的库里会混着理财、技术、摄影。理财 agent 不应该被摄影内容干扰。

- 每条 claim 与 source 都带 `domain` 标签（可多值）。
- 向量库按 domain 建**独立 collection**，检索时物理隔离 → 既提升精度也提升速度。
- domain 可自定义，`distill domain add finance` 并配领域词典（复用 `topics.yaml` 机制）。
- Agent 调用时传 `domain: finance`，只在理财子库检索。

**理财 domain 的额外配置**（领域可定制抽取规则，这是设计的可扩展点）：

```yaml
domains:
  finance:
    extract_numeric: true            # 强制抽取数字与单位
    require_as_of: true              # 无时间标注的 claim 降置信度
    authority_weights:               # 来源权威度加权
      official: 1.0                  # 央行、交易所、监管
      peer_reviewed: 0.9
      media: 0.6
      self_media: 0.4                # 自媒体理财内容大幅降权
    default_max_staleness_days: 180  # 半年前的理财结论默认标记为陈旧
    disclaimer: true                 # 输出强制附免责声明
```

---

## 4. MCP Server（首选接入方式）

MCP 规范 **2026-07-28** 版已支持 `outputSchema` + `structuredContent`，服务端声明了 schema 就**必须**返回符合该 schema 的结构化结果，客户端也会校验——这正好满足"agent 拿到强类型知识"的需求，因此优先走 MCP。

### 暴露的 Tools

| Tool | 用途 | 关键设计 |
|------|------|---------|
| `kb_search` | 语义+关键词混合检索 claims | 入参含 `domain`、`claim_types`、`max_staleness_days`、`min_authority`、`token_budget` |
| `kb_get_claims` | 取某主题下全部主张 | **自动分组为「共识 / 分歧 / 存疑」** |
| `kb_ask` | 基于知识库问答 | 返回答案 + 强制引用 + 冲突提示 |
| `kb_get_source` | 取某篇完整浓缩结果 | 支持 `level: tldr\|full` |
| `kb_list_conflicts` | 列出某主题下的观点冲突 | agent 决策前的必查项 |
| `kb_timeline` | 某主题的观点演进时间线 | 判断"最新共识是什么" |
| `kb_stats` | 库内某 domain 的覆盖情况 | 让 agent 知道**知识边界在哪** |
| `kb_ingest` | 提交新内容消化 | 长耗时 → 用 MCP **Tasks 扩展**异步执行 |

设计要点：
- 除 `kb_ingest` 外全部为**只读**工具，通过 tool annotations 标注（注意：规范要求客户端把 annotations 视为不可信，所以服务端侧也要真正只读，不能只靠声明）。
- `tools/list` 返回**确定性顺序**，配合 `ttlMs` / `cacheScope`，提升客户端与 LLM 的 prompt cache 命中率。
- 视频消化耗时可达数分钟，`kb_ingest` 走 Tasks 扩展返回句柄轮询，避免连接超时。
- 无状态设计：跨调用状态用显式 handle 传递（符合本版规范对有状态工具的指引）。

### `kb_search` 的 outputSchema（关键契约）

```jsonc
{
  "type": "object",
  "properties": {
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "claim_id":   { "type": "string" },
          "subject":    { "type": "string" },
          "predicate":  { "type": "string" },
          "claim_type": { "enum": ["fact","opinion","prediction","method","data"] },
          "numeric":    { "type": "object" },
          "as_of":      { "type": "string", "format": "date" },
          "staleness_days": { "type": "integer" },   // ⭐ 强制返回
          "authority":  { "enum": ["official","peer_reviewed","media","self_media","anonymous"] },
          "confidence": { "type": "number" },
          "conflicts":  { "type": "array" },          // ⭐ 冲突 claim 列表
          "citation":   {                             // ⭐ 强制可溯源
            "type": "object",
            "properties": {
              "source_id": {"type":"string"}, "title": {"type":"string"},
              "url": {"type":"string"}, "anchor": {"type":"string"},
              "author": {"type":"string"}, "publish_at": {"type":"string"}
            }
          }
        },
        "required": ["claim_id","predicate","claim_type","staleness_days","authority","citation"]
      }
    },
    "coverage": {                        // ⭐ 知识边界，防止 agent 在空库上臆断
      "type": "object",
      "properties": {
        "total_matched": {"type":"integer"},
        "domain_source_count": {"type":"integer"},
        "newest_as_of": {"type":"string"},
        "sufficiency": {"enum":["sufficient","thin","empty"]}
      }
    },
    "warnings": { "type": "array", "items": {"type":"string"} }
  },
  "required": ["claims","coverage"]
}
```

`required` 里强制包含 `staleness_days`、`authority`、`citation`——**让 agent 不可能拿到"没有出处、没有时效、没有权威度"的裸结论**。这是从 schema 层面防止误用，比在文档里写注意事项有效得多。

`coverage.sufficiency` 同样重要：库里没有相关知识时明确返回 `empty`，agent 才知道该说"我的知识库里没有依据"，而不是硬编一个答案。

按规范要求，返回 `structuredContent` 的同时也在 `content` 里放一段人类可读摘要，兼顾调试与向后兼容。

---

## 5. HTTP API 与 Python SDK

**HTTP API**（FastAPI，与 M5 的 Web UI 共用后端，附自动生成的 OpenAPI 文档）：
```
GET  /v1/claims/search?domain=finance&q=...&max_staleness_days=180
GET  /v1/claims/{id}
GET  /v1/topics/{topic}/conflicts
GET  /v1/topics/{topic}/timeline
POST /v1/ask
POST /v1/ingest          → 返回 task_id
GET  /v1/tasks/{task_id}
GET  /v1/stats?domain=finance
```
- 本地默认 `127.0.0.1` 绑定；对外暴露时需 token 鉴权，且只读与写入 token 分离。
- 返回体与 MCP 的 outputSchema **共用同一套 Pydantic 模型**，保证两个通道的契约一致（单一数据源，避免两边漂移）。

**Python SDK**（同进程直接调用，理财 agent 若也用 Python 则这是最快路径）：
```python
from distiller import KnowledgeBase

kb = KnowledgeBase("~/insight-distiller/data")
res = kb.search("长期国债配置", domain="finance",
                claim_types=["fact", "data"],      # 只要事实，排除观点
                max_staleness_days=90,
                min_authority="media",
                token_budget=2000)                  # 按 token 预算自动裁剪

if res.coverage.sufficiency == "empty":
    ...                                             # 知识库无依据，agent 明确降级
for c in res.claims:
    print(c.predicate, c.staleness_days, c.citation.url)

kb.conflicts("长期国债")                             # 决策前查分歧
```

`token_budget` 是个实用细节：agent 上下文有限，让知识库**自己按预算裁剪并保留最高价值 claim**，比让 agent 截断字符串合理得多。

---

## 6. 理财 agent 的实际调用范式

```
理财 agent 收到用户问题
  ↓
① kb_stats(domain=finance) → 先确认知识边界，知道自己有多少底气
  ↓
② kb_search(claim_types=["fact","data"], max_staleness_days=90)
     → 只取时效内的客观事实用于计算
  ↓
③ kb_list_conflicts(topic) → 检查是否存在观点分歧
  ↓
④ kb_search(claim_types=["opinion"]) → 需要时单独取观点，标注为"某人观点"
  ↓
⑤ 组织回答：事实与观点分层呈现，标注时效，附出处，有分歧则如实告知
```

**关键：事实与观点分两次取、分层用。** 这个范式让理财 agent 从"复述自媒体口径"变成"基于可溯源事实推理，并如实呈现分歧"。

### 一个必须明确的边界

本工具提供的是**「你的知识库里有谁说过什么、什么时候说的、可信度如何」**，不是投资建议。
所以：
- `finance` domain 默认开启 `disclaimer`，输出附免责声明；
- `claim_type` 强制区分，禁止把 `opinion` / `prediction` 当 `fact` 返回；
- 不设计任何"给出买卖建议"的接口——这类判断应由 agent 与你自己承担，知识库只负责提供带标注的证据。

这不只是合规考虑，更是工程正确性：把预测当事实用，是量化与投研里最常见的亏钱方式。

---

## 7. Claim 抽取的成本

Claim 抽取是额外 LLM 开销，控制方式：
- **不是所有内容都抽**：仅对已配置 domain 且 `density_score` 达标的内容抽取（水文没有可用 claim）。
- **复用 map 阶段**：在原有 map-reduce 的 map 步骤里顺带输出 claim 草稿，不额外增加一轮调用。
- **可延迟**：`distill claims extract --domain finance` 支持事后批量补抽，不阻塞主流程。
- 数字与日期用规则+正则做**后处理校验**，LLM 抽错的数字直接丢弃而非将错就错。

---

## 8. 里程碑

| 阶段 | 内容 |
|------|------|
| **M1** | schema 预留 `claims` 表与 `domain` 字段（同样为了避免后期迁移） |
| **M3.5** | 随实体/关系一起实现 claim 抽取 + domain 分区 + 向量库按 domain 隔离 |
| **M6** | MCP Server（`kb_search` / `kb_ask` / `kb_get_claims` / `kb_list_conflicts` 等）+ HTTP API + Python SDK；理财 domain 配置模板 |

MCP 放在最后不是因为不重要，而是它**依赖 claims 与 domain 已经就绪**——提前做只能暴露散文摘要，反而给 agent 喂了不可靠的输入。

---

## 9. 风险与应对

| 风险 | 应对 |
|------|------|
| agent 把 `opinion` 当 `fact` 用 | schema 层面 `claim_type` 与 `authority` 设为 required，无法绕过 |
| 陈旧金融知识误导决策 | `staleness_days` 强制返回 + domain 级默认过滤 + 检索时按 `as_of` 降权 |
| 库里没知识时 agent 硬答 | `coverage.sufficiency` 明确返回 `empty`，并在 `warnings` 里说明 |
| claim 抽取错误（尤其数字） | 规则+正则后处理校验，抽错即丢弃；claim 保留 `segment_ids` 可人工回查 |
| 两个通道契约漂移 | MCP 与 HTTP 共用同一套 Pydantic 模型生成 schema |
| 本地 API 被其他程序意外访问 | 默认仅绑 `127.0.0.1`；读写 token 分离；写接口默认关闭 |
| 观点冲突被平均成"中立结论" | 不做观点合并，`kb_get_claims` 强制分组为共识/分歧/存疑，把分歧原样交给 agent |

# Insight Distiller · 知识浓缩器 技术方案

> 目标：把「视频 / 播客 / 博客」这类低信息密度的内容，压缩成高密度结论，并支持基于原文的 AI 问答（带出处）。
> 本文档为方案阶段产出，暂不编码。

---

## 1. 问题定义

现状痛点：
- 一个 60 分钟视频 / 5000 字博客，真正有效信息可能只有 300 字。
- 人工筛选成本高，且看完之后无法检索、无法追问。

因此工具要解决三件事：

| # | 能力 | 说明 |
|---|------|------|
| 0 | **自然语言选材** | 输入不限于 URL，可直接说"某 UP 主摄影方向最近半年播放量前 20"、"某专栏收藏量前 100"；工具自行解析、检索、筛选，歧义或量级过大时反问确认。详见 `DISCOVERY.md` |
| 1 | **统一摄取** | URL / 本地文件 / **学术论文（arXiv、DOI、PDF）** 丢进去，都变成带时间戳或章节锚点的文本 |
| 2 | **高密度浓缩** | 不是"摘要"，而是分层输出：结论 → 论据 → 概念 → 金句 → 可执行项，并给出「信息密度评分」帮你判断这内容值不值得看原文。**论文走独立 schema**（问题/方法/实验/局限），详见 `PAPERS.md` |
| 3 | **可追问** | 基于该内容（或跨多个内容）做 RAG 问答，每个回答必须带原文引用与时间戳，可一键跳回原视频位置 |
| 4 | **相似发现** | 丢一篇论文，四路召回融合找出奠基工作 / 同期竞品 / 后续进展 / 跨领域启发，并标注哪些已在你库中。详见 `PAPERS.md` |
| 5 | **知识管理与关联** | 新内容入库自动与历史比对：引用、冲突、迭代、回答了你的旧疑问；配重温机制与知识盲区分析。详见 `KNOWLEDGE.md` |
| 6 | **Agent 可消费** | 知识以**原子 claim**形态开放（区分事实/观点/预测，带时效与出处），通过 MCP / HTTP / SDK 供外部 agent（如你的理财 agent）调用。详见 `AGENT_API.md` |

**四条关键设计原则：**
- **一切结论可溯源** —— 摘要每一条都挂原文片段 ID，避免 LLM 编造。
- **一切自动决定可见** —— 工具替你做的每个假设（时间范围、主题判定、排序补偿、关系判定）都显式展示，且执行前先预演成本。
- **入库即连接** —— 关联不靠你手动打标签，消化流程最后一步自动完成，否则知识库必然沦为写入即遗忘的坟场。
- **对外输出带标注** —— 给 agent 的每条知识强制携带时效、权威度与出处，从 schema 层面杜绝"把自媒体观点当客观事实"。

---

## 2. 整体架构

```
  自然语言指令 ────▶ ┌──────────────────────────────────────────┐
  「某UP主摄影方向     │  0. Discover 发现层（详见 DISCOVERY.md） │
   收藏量前20」        │   意图解析 → 歧义反问 → 平台检索 →       │
                    │   主题漏斗 → 排序 → 成本预演确认          │
                    └────────────────┬─────────────────────────┘
                                     │ 候选 URL 清单
   URL / 文件 ──────────────────────▶┤
   论文(arXiv/DOI/PDF) ─────────────▶┤
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  1. Ingest 采集层（按 content_type 分流） │
                    │   bilibili / wechat / zhihu / xhs / web / │
                    │   local-av / pdf / **paper**（引文元数据） │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  2. Transcribe 转写层（音视频）           │
                    │   优先抓官方字幕 → 无字幕则 ASR          │
                    │   faster-whisper + VAD (+ 说话人分离)    │
                    │   论文走 GROBID/pymupdf 分节抽取          │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  3. Normalize 清洗层                     │
                    │   去口水词 / 重标点 / 语义分段 / 段落锚点 │
                    └────────────────┬─────────────────────────┘
                                     ▼
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
  ┌────────────────────────┐              ┌────────────────────────────┐
  │ 4. Distill 浓缩层      │              │ 5. Index 索引层            │
  │  map-reduce 分层摘要   │              │  chunk + embedding → 向量库 │
  │  密度评分 / 去水       │              │  实体抽取与归一             │
  │  论文用独立 schema     │              │                            │
  └───────────┬────────────┘              └─────────────┬──────────────┘
              ▼                                          ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │ 6. Store  SQLite（内容/片段/浓缩/**实体/关系/疑问/重温**）          │
  │           + LanceDB（向量）                                        │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │ 6.5 Link 关联层（详见 KNOWLEDGE.md）                               │
  │  入库即比对历史：引用/冲突/迭代/回答旧疑问                          │
  │  三级漏斗控成本：引文API(免费) → 向量召回(本地) → LLM判定(仅Top5)   │
  │  抽取原子 claim（按 domain 分区，带时效/权威度/出处）               │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 ▼
  ┌──────────────────────────────┴─────────────────────────────────────┐
  ▼                                                                    ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│ 7. 人类接口                      │   │ 8. Agent 接口（AGENT_API.md）    │
│  CLI（M1）→ Web UI（M5）        │   │  MCP Server（首选，强类型 schema）│
│  ask / paper similar / graph /  │   │  HTTP API + Python SDK           │
│  review / gaps / digest         │   │  → 理财 agent 等外部消费方        │
└─────────────────────────────────┘   └──────────────────────────────────┘
```

---

## 3. 各层选型

### 3.1 采集层（已按你的来源优先级排定）
| 来源 | 优先级 | 方案与难点 |
|------|:--:|------|
| **B 站** | P0 | `yt-dlp` 下载；**优先走 B 站字幕接口**（`player/v2` 返回 CC 字幕或 AI 字幕，命中即跳过 ASR）；UP 主自制视频常无字幕 → 落 ASR。需 cookie 才能取高清与部分字幕 |
| **公众号** | P0 | `mp.weixin.qq.com` 静态可抓，`trafilatura` 抽正文。**难点：大量文章正文是长图** → 检测正文文字量异常低时自动对图片跑 OCR（macOS 原生 Vision / `ocrmac`，中文准确率高、无需下模型） |
| **知乎** | P1 | 有登录墙与折叠答案；`cookies_from_browser` 复用 Chrome 登录态；专栏文章比回答好抓 |
| **小红书** | P1 | 反爬最强。文字笔记走网页抽取；视频笔记需带 cookie 拿真实播放地址，`yt-dlp` 支持不稳 → 单独 adapter + 失败降级手动粘贴 |
| 通用博客 / 新闻 | P1 | `trafilatura` → 降级 `readability-lxml` → 降级 `playwright` 渲染 |
| 本地音视频 / PDF | P2 | `ffmpeg` 统一转 16k 单声道 wav；PDF 走 `pymupdf` |
| **兜底通道** | P0 | 任何来源失败都能 `distill add --paste` 手动贴文本或 `--file` 传本地文件，**采集失败绝不阻断主流程** |

> 反爬是长期战场，因此设计上把「采集」与「浓缩」彻底解耦：拿到文本就能跑后续全链路。

> Adapter 统一接口：`fetch(url) -> RawContent{type, title, author, publish_at, duration, text?, audio_path?, subtitles?}`，新增站点只加一个文件。

### 3.2 转写层
- **`faster-whisper` + `large-v3`**，M5 Pro 上跑 CTranslate2 后端，1 小时音频约 3–6 分钟，纯本地、零成本、隐私安全。
- `silero-vad` 先切掉静音，减少 30%+ 计算量并避免 Whisper 幻觉重复。
- 说话人分离（`pyannote`）标为**可选开关**，访谈类内容才开——它是整条链路最慢的一环。
- 中英混说场景：指定 `language=None` 自动检测 + `initial_prompt` 注入领域术语，显著降低专有名词错字。

### 3.3 浓缩层（核心竞争力）
两阶段 **map-reduce**：
1. **Map**：按语义分块（~2000 tokens，重叠 200），每块提取「本块论点 + 论据 + 术语 + 原文金句 + 片段 ID」。
2. **Reduce**：合并去重，输出统一 schema：

```jsonc
{
  "tldr": ["结论1", "结论2", "结论3"],          // 30 秒读完，最多 5 条
  "outline": [                                   // 分层大纲，可跳转
    { "title": "…", "points": ["…"], "anchor": "00:12:34", "chunk_ids": [3,4] }
  ],
  "concepts": [ { "term": "…", "definition": "…", "anchor": "…" } ],
  "quotes":   [ { "text": "原文金句", "anchor": "…" } ],
  "actions":  ["可执行的 takeaway"],
  "open_questions": ["作者没讲清/存疑的点"],
  "contrarian": ["与主流认知相反的观点"],        // 这类信息价值最高，单独抽
  "density": {
      "score": 0.0,          // 0-100 信息密度
      "compression_ratio": 0.0,
      "verdict": "值得读原文 / 看浓缩版就够 / 纯水文可丢弃"
  }
}
```

密度评分算法（不依赖 LLM 主观打分，可复现）：
`score = f(唯一实体密度, 数字与事实密度, 论证结构完整度, 有效句占比, 重复率惩罚, 营销/口水词惩罚)`
→ 这是本工具区别于"随便丢给 AI 总结一下"的关键：**它先告诉你这内容配不配你花时间**。

**LLM 选型：全面可配置（已确认）**

不锁死任何供应商，配置粒度到「单个任务」：

```yaml
tasks:
  map:    { provider: ollama,   model: chat }    # 分块提取，量大 → 本地免费
  reduce: { provider: deepseek, model: reason }  # 汇总成结论 → 要质量
  qa:     { provider: deepseek, model: reason }  # 问答
```

- 统一 `LLMProvider` 抽象层，内置三种适配：`openai_compat`（DeepSeek/OpenAI/Kimi/任意兼容端点）、`anthropic`、`ollama`。加新供应商只需在 `providers` 里加一段配置，**不改代码**。
- **默认 DeepSeek**（中文强、价格约为 OpenAI 的 1/20，适合大批量消化）。
- 提供 `profiles` 一键整体切换：`local`（全本地零成本）/ `hybrid`（Map 本地 + Reduce 走 API）/ `quality`（全高端模型）。命令行 `distill add URL --profile local` 可临时覆盖。
- **自动降级**：API 超时、报错或月度预算超限时，自动 fallback 到 Ollama，任务不中断。
- **成本可视**：每个 provider 配 `price_per_1m`，每次任务记录 token 与花费，`distill cost` 查本月账单。

详见 `config.example.yaml`。

### 3.4 存储与索引
- **SQLite**（`sources` / `segments` / `chunks` / `digests` / `messages`）：单文件、零运维、可直接用 DB 工具查。
- **LanceDB**：本地向量库，嵌入式无服务、支持混合检索，比 Chroma 更稳。
- Embedding：`bge-m3`（中英双语最优、支持长文本）本地跑。
- 检索策略：**向量 + BM25 混合召回 → rerank（`bge-reranker-v2-m3`）→ 送 LLM**。纯向量检索在专有名词/数字上召回差，混合是必须的。

### 3.5 问答层
- 单篇模式：`ask <source_id> "问题"`，上下文=该篇全部 chunk（长文用检索裁剪）。
- 跨库模式：`ask "问题"`，在整个知识库检索，答案标注来自哪篇哪个时间点。
- 强约束 prompt：**只能基于给定片段作答；无依据必须回答"原文未提及"**，每句话后附 `[S3:12:34]` 形式引用。
- 多轮对话历史存 SQLite，可 `resume` 继续追问。

---

## 4. 项目结构

```
insight-distiller/
├── PLAN.md                    # 本文档（总体方案）
├── DISCOVERY.md               # 自然语言选材层设计
├── DECISIONS.md               # 决策记录
├── README.md
├── pyproject.toml             # 用 uv 管理依赖
├── config.example.yaml        # 模型/API Key/路径配置
├── src/distiller/
│   ├── cli.py                 # Typer 命令行入口
│   ├── config.py
│   ├── models.py              # Pydantic 数据模型
│   ├── pipeline.py            # 编排：ingest→transcribe→distill→index
│   ├── discover/              # 自然语言选材（详见 DISCOVERY.md）
│   │   ├── parser.py          # 人话 → Query IR
│   │   ├── clarify.py         # 歧义检测与反问
│   │   ├── resolver.py        # 实体消歧：名字 → 平台 ID
│   │   ├── providers/         # bilibili / zhihu / xhs / rss
│   │   ├── topic.py           # 两阶段主题判定漏斗
│   │   ├── ranker.py          # 排序 + 平台能力补偿
│   │   ├── planner.py         # dry-run 成本预估
│   │   └── topics.yaml        # 用户主题词典
│   ├── ingest/
│   │   ├── base.py            # Adapter 抽象 + 注册表
│   │   ├── bilibili.py        # P0 含字幕接口
│   │   ├── wechat.py          # P0 公众号 + 图片 OCR
│   │   ├── zhihu.py  xhs.py   # P1 需 cookie
│   │   ├── web.py             # 通用兜底
│   │   ├── local.py  pdf.py  paste.py
│   │   └── ocr.py             # macOS Vision OCR
│   ├── paper/                 # 学术论文模块（详见 PAPERS.md）
│   │   ├── resolve.py         # arXiv/DOI/标题 → 唯一 paper_id
│   │   ├── clients/           # semantic_scholar.py openalex.py arxiv.py crossref.py
│   │   ├── parse.py           # PDF 分节抽取（pymupdf4llm → GROBID 降级）
│   │   ├── similar.py         # 四路召回 + RRF 融合
│   │   ├── lineage.py         # 引文拓扑 → 阅读路径
│   │   ├── compare.py         # 多篇对比表 / 小型综述
│   │   └── cache.py           # 引文元数据缓存（30 天）
│   ├── transcribe/
│   │   ├── whisper_local.py   subtitle.py   vad.py   diarize.py
│   ├── distill/
│   │   ├── chunker.py         # 语义分块
│   │   ├── summarizer.py      # map-reduce
│   │   ├── density.py         # 信息密度评分
│   │   └── prompts/           # 按 content_type 分：video/ article/ paper/
│   ├── knowledge/             # 知识管理与关联（详见 KNOWLEDGE.md）
│   │   ├── entity.py          # 实体抽取
│   │   ├── normalize.py       # 实体归一（别名合并）
│   │   ├── linker.py          # 三级漏斗关系判定
│   │   ├── claims.py          # 原子 claim 抽取与校验
│   │   ├── domain.py          # 领域分区与领域规则
│   │   ├── graph.py           # 图谱遍历（SQLite 递归 CTE）
│   │   ├── review.py          # 间隔重复调度
│   │   ├── gaps.py            # 知识盲区分析
│   │   └── aliases.yaml       # 用户别名词典
│   ├── agent/                 # Agent 接口（详见 AGENT_API.md）
│   │   ├── schemas.py         # ⭐ Pydantic 契约，MCP 与 HTTP 共用
│   │   ├── service.py         # 检索/冲突/覆盖度等核心查询
│   │   ├── mcp_server.py      # MCP Server（2026-07-28 规范）
│   │   ├── http_api.py        # FastAPI + OpenAPI
│   │   └── sdk.py             # Python SDK（同进程调用）
│   ├── llm/
│   │   ├── base.py  ollama.py  openai_compat.py  anthropic.py
│   ├── store/
│   │   ├── db.py  schema.sql  vector.py
│   ├── qa/
│   │   ├── retriever.py       # 混合检索 + rerank
│   │   └── chat.py
│   └── render/
│       ├── markdown.py        # 导出 Obsidian 友好 md（含双链）
│       └── web/               # M5 的 FastAPI + 前端
├── data/                      # 运行时数据（gitignore）
│   ├── distiller.db   audio/   transcripts/   digests/   papers/   lancedb/
└── tests/
```

---

## 5. 使用形态（CLI 先行）

```bash
# ① 单条消化（自动判断字幕/ASR）
distill add "https://www.bilibili.com/video/BVxxxx"

# ② 自然语言选材：默认只发现候选、不消化（详见 DISCOVERY.md）
distill find "影视飓风摄影方向最近半年播放量前20"
distill find "某专栏收藏量前100" --run          # 确认后批量消化
distill plan "…"                                # 只看工具如何理解你的话

# 只看结论
distill show 12 --level tldr

# 完整浓缩版（含大纲、金句、时间戳）
distill show 12

# 追问
distill ask 12 "他说的第二个反直觉观点，论据是什么？"

# 跨库提问
distill ask "我看过的内容里，关于 RAG 召回优化有哪些不同做法？"

# 批量消化 + 每日汇总 + 按信息密度排序看哪些值得读
distill batch urls.txt && distill digest --today
distill list --sort density --min-score 60

# ③ 论文（详见 PAPERS.md）
distill add "https://arxiv.org/abs/2501.xxxxx"
distill paper similar 42            # 四路召回：奠基/竞品/后续/跨领域，标注已在库中的
distill paper lineage 42            # 引文拓扑排序 → 该先读哪篇
distill paper compare 42 43 51      # 方法/数据集/指标对比表

# ④ 知识管理（详见 KNOWLEDGE.md）
distill graph 42 --depth 2          # 以某篇为中心的关系网
distill timeline "RAG"              # 某主题的认知演进
distill gaps                        # 知识盲区：被反复引用但你没读过的
distill review                      # 今日重温（间隔重复，只推 TL;DR）
distill entity show RAG             # 这个概念在库里被提过 27 次，各家定义差异

# 成本查询 / 环境自检
distill cost --month
distill doctor

# 导出到 Obsidian（含双链）
distill export 12 --to ~/Obsidian/Inbox

# ⑤ Agent 接口（详见 AGENT_API.md）
distill domain add finance                 # 建理财领域分区
distill claims extract --domain finance    # 抽取原子 claim（事实/观点/预测分类）
distill claims search "长期国债" --domain finance --type fact --max-stale 90
distill serve mcp                          # 起 MCP Server，供理财 agent 接入
distill serve api --port 8787              # 起 HTTP API（默认仅绑 127.0.0.1）
```

---

## 6. 里程碑

| 阶段 | 内容 | 交付标准 |
|------|------|----------|
| **M1 最小闭环** ✅ | Python 环境 + 依赖；配置系统（多 provider / profile / 降级）；**完整 schema**；B 站字幕 + 公众号（含 OCR）+ 通用网页采集；map-reduce 浓缩；`add` / `show` / `list` / `doctor` / `cost` | 丢一个链接直接拿到分层结论 md |
| **M2 音视频 + 问答** ✅ | faster-whisper + VAD 转写（无字幕视频）；SQLite 向量存储 + BM25 混合检索 + RRF 融合；`ask` / `search` / `index` 带引用与时间戳；补知乎 / 小红书 adapter | 无字幕视频也能消化，问答可溯源 |
| **M2.5 自然语言选材** ✅ | Query IR 解析（规则层 + LLM 补语义）+ 歧义反问 + B 站发现器（UP 主投稿/关键词搜索/收藏量本地补齐）+ 主题两阶段漏斗 + dry-run 成本预演 + `find` / `plan` | 说一句话就能挑出并批量消化目标内容 |
| **M3 论文模块** ✅ | 标识解析（arXiv/DOI/PDF/标题）+ 多源元数据融合 + PDF 分节抽取 + 论文专属浓缩 schema + **四路召回相似论文**（引文/被引/文献耦合/语义）+ `lineage` 阅读路径 + `compare` 对比 | 丢一篇论文，拿到精读笔记 + 分组的相似论文推荐 |
| **M3.5 知识关联** ✅ | 实体抽取与归一（变体合并、用户词典）；**claim 抽取 + domain 分区**（数字规则校验）；三级漏斗自动关联；入库即提示；`graph` / `gaps` / `timeline` / `entity` / `claims` / `link` | 新内容入库自动告诉你它和历史内容什么关系 |
| **M4 重温与沉淀** ✅ | 间隔重复调度（SM-2 简化版 + **入队价值筛选**）；批注系统；周期简报（零 LLM）；疑问追踪闭环；Obsidian 双链导出（关系/概念页/MOC） | 沉淀的知识会主动回来找你 |
| **M5 密度评分 + Web UI** ✅ | density 打分（M2 已落地）；**FastAPI + Jinja2 + 原生 JS，零构建步骤**；内容库/阅读/问答/图谱/重温/简报六页；**点时间戳跳回原视频**；手写力导向图谱 | 浏览器里完成全流程 |
| **M6 Agent 接口 + 自动化** ✅ | **MCP Server（outputSchema 由 Pydantic 生成）+ HTTP API `/v1` + Python SDK，三通道共用同一套契约**；时效降权/权威门槛/token 预算裁剪/coverage 诚实判定；共识-分歧-存疑分组；理财 domain 模板；订阅固化 + 定时抓取 + 每日日报 | 理财 agent 能直接查你的知识库，且拿到的每条知识都带时效与出处 |

**排序理由**：
- Discovery（M2.5）放在问答之后 —— 它的价值依赖「消化质量已可信」，否则一次抓 50 个视频才发现摘要不达标，白烧算力。
- 论文（M3）在知识关联（M3.5）之前 —— **引文关系是零成本的确定性关联**，先用它把关系图谱的链路验证通，再上不确定的 LLM 语义判定。
- Agent 接口（M6）在最后 —— 它**依赖 claims 与 domain 已就绪**。提前做只能对外暴露散文摘要，等于给 agent 喂不可靠输入，反而有害。
- **M1 必须一次落全 schema** —— 关系表、实体表、claims 表如果留到后面才加，会触发数据迁移，代价远高于提前设计。

### M1 开工前需装（届时我一并处理）
`brew install ffmpeg python@3.12 uv` → `uv sync` → `yt-dlp`（作为 Python 依赖装）→ 可选 `brew install ollama && ollama pull qwen3:14b`。
系统自带的 Python 3.9.6 不动，项目用 uv 建独立 3.12 虚拟环境，互不干扰。

---

## 7. 主要风险与应对

| 风险 | 应对 |
|------|------|
| 长内容 LLM 丢信息 | map-reduce + 每条结论强制挂 chunk_id，Reduce 阶段做覆盖率校验 |
| ASR 专有名词错字 | `initial_prompt` 注入术语表 + 用户级自定义词典做后处理替换 |
| 平台反爬 / 需登录 | 支持导入浏览器 cookie；失败时降级为手动粘贴，不阻塞主流程 |
| LLM 幻觉编造结论 | 无出处即拒答；输出后做一次「引用回查」验证 |
| API 成本 | Map 用本地模型，Reduce 才用 API；转写结果永久缓存，重跑摘要不重复转写 |
| 版权 | 纯本地个人使用，不做内容再分发 |
| 自然语言指令炸出上百条内容 | dry-run 强制确认 + 月度预算硬护栏 + 默认 Top20（详见 `DISCOVERY.md` §9） |
| 学术 API 限流（S2 有 key 也仅 1 req/s） | 引文元数据缓存 30 天 + 指数退避 + 降级 OpenAlex；OpenAlex 用 OR 语法合并 50 ID/请求（详见 `PAPERS.md` §7） |
| 关联提示噪音大变成骚扰 | 只提示 confidence > 0.7 且每篇上限 5 条；用户可否决，误判模式后续降权（详见 `KNOWLEDGE.md` §9） |
| 知识库沦为"写入即遗忘" | 入库即自动建立关联 + 被动唤醒（新内容提示相关旧内容）+ 轻量间隔重复，不依赖用户手动整理 |
| 数据被锁死在本工具里 | 单文件 SQLite 可备份；全部内容可导出为纯 Markdown / Obsidian 双链 |
| Agent 把自媒体观点当客观事实使用 | claim 强制标注 `claim_type` / `authority` / `staleness_days`，schema 层面设为 required（详见 `AGENT_API.md` §9） |
| 陈旧金融知识误导 agent 决策 | 强制返回 `staleness_days` + 领域级默认时效过滤 + 按 `as_of` 降权 |
| 库内无相关知识时 agent 硬答 | 返回 `coverage.sufficiency = empty`，让 agent 明确降级而非臆断 |

---

## 8. 选型结论（已确认）

| 项 | 结论 |
|----|------|
| LLM 路线 | 全面可配置，任务级指定 provider，附 `local` / `hybrid` / `quality` 三套预设 |
| 默认 API | DeepSeek，可随时切 OpenAI / Claude / Kimi / Ollama |
| 内容来源 | P0：B 站、公众号、通用网页；P1：知乎、小红书；**论文：arXiv / DOI / 本地 PDF** |
| 输入形态 | URL、本地文件、论文标识、**自然语言选材指令** |
| 交互形态 | CLI 先行，网页版排在 M5 |
| 对外接口 | **MCP Server（首选，2026-07-28 规范）+ HTTP API + Python SDK**，共用同一套 Pydantic 契约 |
| 学术数据源 | OpenAlex（元数据与引文图谱主力，免费额度充裕）+ Semantic Scholar（相似推荐，需重缓存）+ arXiv + Crossref |

## 9. 配套文档

| 文档 | 内容 |
|------|------|
| `DISCOVERY.md` | 自然语言选材：意图解析、反问策略、平台能力约束、成本预演 |
| `PAPERS.md` | 论文模块：解析、独立 schema、四路召回相似检索、阅读路径 |
| `KNOWLEDGE.md` | 知识管理：数据模型、关系类型、自动关联漏斗、重温机制 |
| `AGENT_API.md` | Agent 友好：原子 claim、领域分区、MCP / HTTP / SDK 三通道 |
| `DECISIONS.md` | 决策记录与关键取舍 |
| `config.example.yaml` | 全部可配置项 |

方案已定稿。说一声「开工」我就按 M1 开始搭：环境 → 配置系统 → 完整 schema → B 站/公众号采集 → map-reduce 浓缩 → `add` / `show` 跑通。
```

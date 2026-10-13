# Insight Distiller · 知识浓缩器

把视频、播客、博客、论文这类**低信息密度内容**压缩成高密度结论，并支持基于原文的可溯源 AI 问答。

> 当前状态：**全部里程碑完成（M1–M6）** —— 选材 → 采集 → 转写 → 浓缩 → 索引 → 问答 → 论文 → 知识关联 → 重温沉淀 → 网页版 → Agent 接口

---

## 它解决什么问题

一个 60 分钟视频、5000 字博客，真正有效信息可能只有 300 字。本工具：

1. **说人话就能选材** —— 「B站 某UP主 摄影方向 最近半年 播放量前20」直接变成候选清单，歧义时反问，**执行前预演成本**
2. **先告诉你值不值得看** —— 可复现的信息密度评分（不用 LLM 主观打分），给出「值得读原文 / 看浓缩版就够 / 疑似水文」
3. **分层浓缩而非摘要** —— 核心结论 → 反直觉观点 → 带锚点的大纲 → 概念表 → 金句 → 可执行项 → 存疑点
4. **无字幕也能消化** —— 优先抓官方字幕（快约 50 倍且免费），无字幕才走本地 Whisper
5. **可追问且可溯源** —— 混合检索后问答，回答带引用与时间戳；**资料没提到的会明确说明，不编造**
6. **论文精读与脉络梳理** —— 独立的「问题/方法/实验/局限」schema；四路召回相似论文；自动生成阅读路径
7. **入库即自动关联** —— 新内容会主动告诉你它和历史内容什么关系：**引用了谁、和谁结论冲突、是谁的后续、回答了你哪个旧疑问**
8. **沉淀的知识会回来找你** —— 只调度**值得重温**的内容；每周简报汇总悬着的待办与新发现的冲突；可导出成 Obsidian 双链库
9. **浏览器里完成全流程** —— `distill serve` 打开本地网页版，**点时间戳直接跳回原视频那一秒**；关系图谱可交互
10. **能被外部 Agent 当知识后端** —— 知识以**原子 claim** 形态开放，每条强制携带时效、权威度与出处；MCP / HTTP / SDK 三通道
11. **订阅自动化** —— 把自然语言选材固化成定期任务，每天自动抓取浓缩入库

---

## 快速开始

```bash
# 1. 创建环境（Python 3.11+）
python -m venv .venv
.venv/bin/pip install -e ".[search]"    # Windows: .venv\Scripts\pip install -e ".[search]"

# 2. 配置模型（默认 DeepSeek，可换任意供应商）
cp config.example.yaml config.yaml
export DEEPSEEK_API_KEY=sk-xxx          # Windows: set DEEPSEEK_API_KEY=sk-xxx

# 3. 自检
.venv/bin/distill doctor

# 4. 消化并追问
.venv/bin/distill add "https://mp.weixin.qq.com/s/xxxxx"
.venv/bin/distill ask "这篇讲的核心方法是什么？"
```

### 可选能力

```bash
pip install -e ".[search]"  # 语义检索与问答（建议装，M2 核心）
pip install -e ".[ocr]"     # 公众号图片型文章 OCR（强烈建议装）
pip install -e ".[av]"      # 无字幕音视频的本地转写
pip install -e ".[web]"     # 网页版
pip install -e ".[agent]"   # MCP / HTTP API
pip install -e ".[all]"     # 全部
pip install -e ".[dev]"     # 测试
```

---

## 迁移到 Windows

```bash
# macOS 侧打包
python scripts/package.py --with-data     # 约 400KB + 知识库大小
```

```powershell
# Windows 侧：解压后
winget install Python.Python.3.12         # 若还没有 Python
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

安装脚本会自动校验 Python 版本、清理误拷的 macOS 虚拟环境，并**根据有无 NVIDIA 显卡选择 PyTorch 版本**（没独显时装 CPU 版，省 2.3GB 下载）。

打包脚本会排除 `.venv/`（含 macOS 二进制）、`config.yaml`（含 API Key）与音频缓存，但**会带上 `data/distiller.db`** —— 库内只存相对路径，拷过去直接可用，历史消化结果零转换。到目标机后只需 `distill index --rebuild` 重建向量索引。

已实测：干净目录解压 + Python 3.14 全新安装 → `doctor` 全绿 + 339 项测试通过。
详见 `PORTABILITY.md`（含打包时发现的两个真实缺陷记录）。

---

## 常用命令

```bash
# 自然语言选材（默认只列候选，不消化）
distill plan "B站 影视飓风 摄影方向 最近半年 播放量前20"   # 只看工具如何理解，零成本
distill find "B站关于RAG的视频前10"                        # 出候选表 + 成本预演
distill find "..." --run                                    # 确认后批量消化
distill find "..." --run --exclude 3,7                      # 剔除第3、7条再跑
distill find "..." --pick 2                                 # 同名账号时选第2个
distill find "..." --run --yes                              # 脚本化，跳过确认

# 消化
distill add <URL>                      # B站/公众号/知乎/小红书/任意网页
distill add - --paste                  # 反爬兜底：手动粘贴正文
distill add x --file notes.txt          # 从本地文本导入
distill add <URL> --profile local       # 临时全用本地模型（零成本）

# 查看
distill list                            # 已消化内容
distill list --sort density_score --min-score 60   # 只看高密度的
distill show 1                          # 完整浓缩版
distill show 1 --level tldr             # 只看核心结论
distill show 1 --export ~/Obsidian      # 导出

# 检索与问答
distill search "关键词"                  # 混合检索原文（不调 LLM，零成本）
distill ask "问题"                       # 跨库问答，带引用
distill ask "问题" --source 3            # 只问某一篇
distill ask "问题" --show-context        # 同时显示检索到的原文
distill index                            # 补建缺失索引

# 论文
distill add "https://arxiv.org/abs/1706.03762"   # 精读（自动下 PDF + 分节）
distill add ~/papers/foo.pdf                      # 本地 PDF
distill paper similar 3                           # 四路召回相似论文，分组展示
distill paper similar "arXiv:2005.11401" --unread # 只看未消化过的
distill paper similar 3 --add 1,4                 # 直接消化推荐的第1、4篇
distill paper lineage 3                           # 阅读路径：该先读哪几篇
distill paper compare 3 5 7                       # 横向对比多篇

# 运维
distill doctor                          # 环境与平台能力自检
distill cost                            # 本月 LLM 花费
```

### 重温与沉淀

```bash
distill review                   # 该回头看什么（含待办、存疑、我的批注）
distill review -g ok --id 12     # 打分：forgot / ok / easy
distill review --star 12         # 标记必看（绕过价值判断）
distill review --rebuild         # 为历史内容批量判定入队
distill note 12 "我的想法"        # 记批注
distill note 12 -t done "补上BM25"  # 标记待办完成，简报不再提醒
distill brief                    # 周期简报（零 LLM 成本）
distill brief --export ~/notes   # 导出简报 Markdown
distill questions                # 未解疑问追踪
distill vault ~/Obsidian/知识库   # 导出为 Obsidian 双链库
```

**一个关键判断：不是所有内容都值得重温。**

传统 SRS（Anki 那套）是为「记忆卡片」设计的——每张卡都该记住。但知识内容不是：
水文不值得重温，纯资讯看完就过期。全部入队只会让重温变成负担，最后必然放弃。

因此入队要筛选，优先看这些信号：

| 信号 | 为什么 |
|------|--------|
| 有可执行项 | 重温价值最高——因为你还没做 |
| 有未解疑问 | 可能已经能回答了 |
| 被多篇关联 | 是知识网络的枢纽 |
| 密度 ≥ 65 | 值得反复看 |
| 你 star 了 | 绕过所有判断 |

重温时**只展示 TL;DR + 待办 + 你的批注**，不展示全文——目的是唤起记忆并检查待办，不是重读一遍。

`brief` 也不是罗列「这周看了什么」（那种清单看一次就够），而是汇总你自己想不起来但工具记得的东西：新发现的**结论冲突**、悬着多久的**可执行项**、**旧疑问已有答案**、**知识盲区**。全部由数据库直接算出，**零 LLM 成本**。

`vault` 导出的 Obsidian 库里，关系与概念都是 `[[双链]]`，可直接用图谱视图浏览；可执行项是 checkbox 能直接勾选。这也让知识能脱离本工具存在（避免数据绑定）。

### 网页版

```bash
pip install -e '.[web]'    # 只需 fastapi + uvicorn + jinja2
distill serve              # 默认 http://localhost:8765，自动开浏览器
distill serve --reload     # 开发模式，改代码自动重启
```

| 页面 | 作用 |
|------|------|
| 内容库 | 密度条可视化、领域/类型分面筛选、按密度排序 |
| 阅读 | 分层浓缩 + **点时间戳跳回原视频那一秒** + 关联侧栏 + 批注 + 可执行项勾选 |
| 问答 | 带引用卡片的回答，可点进出处；支持限定单篇提问 |
| 图谱 | 关系可视化，冲突用红线标出，可拖拽缩放、点节点跳转 |
| 重温 | 到期内容 + 为什么推给你 + 三档打分 |
| 简报 | 与 CLI 同口径，冲突优先呈现 |

**两个刻意的技术选择：**

**不用 React/Vue。** SPA 需要 Node + npm 构建链，在 Windows 上多一整套依赖生态要维护——而这是个单用户本地工具，那点交互复杂度撑不起这个代价。改用 Jinja2 服务端渲染 + 原生 JS，**零构建步骤**，`pip install` 完就能跑，改完刷新即见效。

**不用 d3 画图谱。** 力导向只需要斥力、弹簧、向心力三条规则，手写百来行就够，省掉 250KB 依赖。

深链是网页版相对 CLI 的核心增量：CLI 里看到 `02:05` 只能自己拖进度条，网页里点一下就跳到 `bilibili.com/video/BVxxx?t=125`。**各平台时间参数格式不同且不能猜**——写错会静默跳到视频开头而不报错，所以只对已确认格式的平台做转换（B 站、YouTube、本地文件），其余原样返回原文链接。

### Agent 接口

```bash
pip install -e '.[agent]'
distill serve-mcp                 # MCP Server（stdio），供 Claude Desktop 等接入
distill serve-mcp --tools         # 只看工具清单
distill serve-agent               # HTTP API /v1，默认仅绑 127.0.0.1
distill domain list               # 查看领域分区
distill domain template           # 打印理财领域配置模板
```

**核心判断：给人看的散文摘要，和给 agent 用的知识，是两种不同的产物。** 只加个 API 并不等于 agent 友好。

假设理财 agent 问「该不该买长债」，如果知识库只返回一段摘要，agent 拿到的是有害输入——不知道是谁说的、什么时候说的、库里有没有相反观点。所以关键不是传输协议，而是**知识的形态**：

```python
from distiller import KnowledgeBase

with KnowledgeBase() as kb:
    kb.stats("finance")                      # ① 先确认知识边界
    kb.search("长期国债", domain="finance",   # ② 只取事实用于推理
              claim_types=["fact", "data"],
              max_staleness_days=90,
              token_budget=2000)
    kb.conflicts("长期国债")                  # ③ 决策前查分歧
    kb.search("长期国债", claim_types=["opinion"])  # ④ 观点单独取、标注来源
```

**事实与观点分两次取、分层用** —— 这个范式让 agent 从「复述自媒体口径」变成「基于可溯源事实推理，并如实呈现分歧」。

每条返回强制携带这些字段（在 Pydantic schema 里设为 `required`，**从类型层面杜绝裸结论**）：

| 字段 | 防的问题 |
|------|---------|
| `claim_type` | 不区分 fact/opinion/prediction，agent 必然把自媒体观点当事实 |
| `staleness_days` | 金融知识会过期，2024 年的利率判断放到 2026 年是错的 |
| `authority` | 官方公告与自媒体不该等权 |
| `citation` | 无出处的结论无法核实 |
| `coverage.sufficiency` | 库里没知识时返回 `empty`，agent 才知道该说「我没有依据」而不是硬编 |

三个通道（MCP / HTTP / SDK）**共用同一套 Pydantic 契约**，物理上不可能漂移。MCP 的 `outputSchema` 直接从模型类生成，客户端会据此校验。

安全默认值：仅绑 `127.0.0.1`；读写 token 分离；写接口默认关闭（关闭时 MCP 连 `kb_ingest` 都不暴露）；绑非本机却没设 token 时**拒绝启动**而非默默暴露。

### 订阅自动化

```bash
distill subscribe add finance "B站 财经UP主 理财方向 最近内容" \
    --domain finance --every 24 --max 3 --min-density 50
distill subscribe run --dry-run        # 先看会抓什么
distill subscribe run --export ~/notes # 跑完导出日报
distill subscribe history finance      # 排查「为什么这条没抓到」
```

挂到 cron 或任务计划程序里即可。三条约束都是为了**防止自动化变成失控的烧钱机器**：单次条数上限、月度预算检查、密度门槛（低质内容标记后不再重复抓）。

注意订阅运行时**无法反问**（没人在旁边），所以指令要先用 `distill plan` 确认能被正确理解。

### 知识关联

消化新内容时会自动与历史比对，并直接告诉你发现了什么：

```
$ distill add "https://..."
✓ #12 「混合检索的必要性」
  信息密度 68/100 · 信息密度高，值得读原文

⚡ 与你的知识库发现 2 处关联
  ├─ ⚠️ 结论冲突 #3《纯向量检索方案》
  │  本文：向量检索召回率低，必须混合 BM25
  │  对方：纯向量检索已足够，无需 BM25
  └─ 🎯 可回答你在《RAG 实践》留下的疑问：长文档如何分块？

  领域 tech · 12 个概念 · 5 条 claim
```

相关命令：

```bash
distill graph               # 全库关系概览
distill graph 12            # 以某篇为中心的关系网（含多跳）
distill entity              # 高频概念列表
distill entity "RAG"        # 该概念在哪些内容里出现、各自定义
distill entity "RAG" --merge "检索增强"   # 合并同义写法
distill timeline "利率"      # 某主题的认知演进（按主张时间排序）
distill gaps                # 知识盲区：反复提及但没有定义的概念
distill claims --type fact  # 原子知识单元（区分事实/观点/预测）
distill link --all          # 为历史内容补建关联
```

**三个关键设计**：

| 设计 | 说明 |
|------|------|
| **成本不随库规模增长** | 三级漏斗：引文关系与实体共现（纯 SQL，零成本）→ 向量召回 Top20（本地零成本）→ **仅 Top5 交 LLM 一次批量判定**。每篇新内容约 1 次调用，库里有 1000 篇也一样 |
| **关系分类型，不只标「相关」** | `cites` / `contradicts` / `supports` / `extends` / `prerequisite` / `answers` / `duplicate` / `same_topic`。其中 `contradicts` 价值最高——认知升级多来自发现矛盾 |
| **冲突只提示不裁决** | 必须附**双方原文主张**，让你自己判断谁对。缺证据的冲突判定会自动降级为同主题 |

`claims` 是为 M6 的 agent 接口准备的：强制区分 `fact` / `data` / `opinion` / `prediction` / `method`，带 `as_of` 时间标注与结构化数值。**LLM 编造的数字会被规则校验丢弃**——agent 会拿这些数去计算，不能将错就错。

### 论文模块说明

**四路召回**（分组展示，而非平铺列表）：

| 分组 | 来源 | 价值 |
|------|------|------|
| 奠基工作 | 本文参考文献中高被引的 | 该先补的前置知识 |
| 同期竞品 | **文献耦合**（共同引用重叠） | 解决同类问题的不同路线 |
| 后续进展 | 引用了本文的，按**年均被引**排序 | 可能已有改进 |
| 跨领域启发 | 语义/关键词检索 | 引文无交集但方法相通，最易漏掉 |

两个关键细节：
- **文献耦合会排除超高被引的通用论文**（Attention、Adam 等）。几乎所有 ML 论文都引用它们，用它们做耦合会召回大量无关工作。
- **后续进展按年均被引排序**而非绝对被引。纯按被引会全是老论文，新的改进工作永远排不上。

`lineage` 会生成阅读顺序，例如对 RAG 论文给出：`Adam → Attention → T5 → BART → GLUE → RAG`，并标注哪些已在你库中。

### 自然语言选材的设计原则

| 原则 | 做法 |
|------|------|
| **只在猜错代价高时反问** | 实体不唯一、语义真歧义（如「收藏量」指被收藏数还是我的收藏夹）、公众号无批量接口 → 阻断问清；时间/数量缺失 → 自行决定但**写进「自动决定的部分」展示** |
| **默认不消化** | `find` 先出候选表（标题/时长/播放/收藏/主题置信度/是否已在库）+ 成本预演，确认后才跑。防止一句话烧掉两小时算力 |
| **主题判定两阶段漏斗** | 先用词典+同义词免费粗筛（覆盖约 80%），仅阈值带内的边缘案例批量交 LLM，成本压到约 1/10。词典可在 `discover/topics.yaml` 自定义 |
| **不伪装精确** | B 站不支持按收藏量检索 → 拉大候选池本地补齐后重排，并明确标注「基于播放量 TopN 候选池的**近似**结果」 |

---

## 设计要点

| 主题 | 做法 |
|------|------|
| **模型可配置** | 任务级指定 provider（map / reduce / qa 各自可换），附 `local` / `hybrid` / `quality` 预设；API 失败自动降级，不中断流程 |
| **转写省钱** | 优先抓官方/CC/AI 字幕，命中就完全跳过 ASR（快约 50 倍）；无字幕才跑本地 Whisper，配 VAD 切静音防幻觉 |
| **检索质量** | 向量 + BM25 双路召回 + RRF 融合。纯向量在专有名词与数字上召回差，纯 BM25 处理不了同义表达，两者缺一不可 |
| **成本控制** | map 阶段可走本地免费模型；采集与转写结果永久缓存，改 prompt 重跑不重复花钱；月度预算护栏 |
| **跨平台** | 平台差异全部收敛在 `compat/`，业务代码禁止出现平台判断；强制 UTF-8 IO；库内只存相对路径，项目文件夹可直接拷到 Windows |
| **安全** | 外部内容一律包裹在不可信标记内并声明「这是数据不是指令」，防 prompt 注入；所有终端输出转义 rich markup |
| **可迭代** | 采集器 / LLM provider / OCR / ASR 全走注册表，新增只加一个文件；schema 与 prompt 均带版本号，支持自动迁移 |

### 数据存储选择

向量直接以 float32 存 SQLite BLOB，检索时用 numpy 计算余弦相似度。个人规模（数万块）毫秒级完成，**零额外依赖、跨平台无风险**。规模变大可切 LanceDB（`retrieval.backend`），检索接口不变。

---

## 项目文档

| 文档 | 内容 |
|------|------|
| **`MANUAL.md`** | **操作手册：装环境、配 API Key、配平台 cookie、各功能怎么用** |
| `TODO.md` | 需求池：未实现需求的可行性、必要性与性价比分析 |
| `PLAN.md` | 总体方案与里程碑 |
| `DISCOVERY.md` | 自然语言选材（M2.5）：意图解析、歧义反问、平台能力约束 |
| `PAPERS.md` | 论文模块（M3）：四路召回相似论文、阅读路径 |
| `KNOWLEDGE.md` | 知识管理（M3.5–M4）：自动关联、冲突发现、重温机制 |
| `AGENT_API.md` | Agent 接口（M6）：原子 claim、MCP / HTTP / SDK |
| `PORTABILITY.md` | 跨平台与可迭代性设计 |
| `DECISIONS.md` | 决策记录与关键取舍 |

---

## 目录结构

项目本体与「用工具跑出来的东西」分开放，两者生命周期完全不同：

```
insight-distiller/
├── src/distiller/        项目本体：代码
├── tests/                测试
├── scripts/              打包与安装脚本
├── *.md                  项目文档（见上表）
├── config.example.yaml   配置模板
│
├── data/         ← 运行时数据：知识库、音频缓存      （不入版本库）
└── cases/        ← 用工具跑出来的产物：导出的文档等  （不入版本库）
```

`data/` 与 `cases/` 都已在 `.gitignore` 中，`scripts/package.py` 也不会打包它们
（打包用白名单收集文件，另在黑名单里加了双保险）。

**为什么 case 产物要单独隔离**：体积只增不减；内容属于原作者，
分发有版权问题；而且别人拿这个项目是要用工具，不是要你的阅读记录。
详见 `cases/README.md`。

换机器时用 `python scripts/package.py --with-data` 把知识库一起带走。

---

## 开发

```bash
.venv/bin/python -m pytest -q     # 465 项测试，不依赖网络与 API Key
```

测试用 fake LLM provider、stub embedder 与 stub 学术客户端覆盖完整链路，因此**无需真实 Key、无需下载模型、无需联网**即可验证，4 秒跑完。
跨平台关键函数与决策逻辑（是否跑 ASR、字幕优先级、指令解析、反问时机、论文章节识别、可信度三态、实体变体合并、claim 数字校验、图遍历防环、重温入队筛选、wikilink 文件名安全、深链参数格式、页面 XSS 转义、契约 required 约束、语义相关性门槛、订阅去重与预算护栏、**中文 BM25 端到端命中**）均有测试守护。

### 升级已有知识库

schema 变更会在启动时自动迁移并备份数据库。若从 v3 及更早版本升级，
关键词索引需重建一次（中文分词规则已修正）：

```bash
distill index --rebuild-fts    # 秒级，不调用 embedding 模型
```

`distill doctor` 会在需要时主动提示。

---

## 已知限制

### 学术 API（实测 2026-09）
- **OpenAlex 已改为付费额度制**：匿名 list / search 请求返回 429（`Insufficient budget`），因此本工具改用 **arXiv 为主力**，OpenAlex 仅作单篇补充。
- **Semantic Scholar 匿名池频繁限流**：已做指数退避 + 30 天缓存。其 `/recommendations` 接口实测稳定返回空，因此**未采用**该路（改用引文图谱，本就更可靠）。
- 建议填 `paper.mailto` 进 polite pool；若需大量分析论文，可申请 S2 API key。
- 任一召回路失败时会**明确告知哪一路不可用**，而非静默返回残缺结果。
- **数据源限流时不会误报可信度**：无法确认发表状态时显示「发表状态未知」，而不是断言「预印本 / 未经同行评审」。

### 其他
- **B 站部分接口需登录态**：关键词搜索可匿名；**UP 主投稿列表**通常需 cookie（配 `ingest.cookies_from_browser: chrome`）。
- **无字幕视频需装 `[av]`**：首次转写会下载 Whisper 模型（`large-v3` 约 3GB）。
- **公众号无批量发现接口**：只能逐篇给 URL，工具会如实告知而非假装能做。
- **小红书风控严格**：建议配 cookie，失败时用 `--paste` 兜底。
- **知乎批量发现尚未实现**：M2.5 只做了 B 站发现器。
- **扫描版 PDF 需 OCR**：当前论文解析只处理文本型 PDF。
- **密度评分系数待真实数据校准**：能可靠区分水文（0 分）与实质内容（论文约 70 分），主要用于相对排序。
- **未做 rerank**：当前是双路召回 + RRF，接口已预留。

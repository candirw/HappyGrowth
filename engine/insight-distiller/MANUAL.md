# 操作手册

从零到能用，大约 15 分钟。**先读「第一步」和「第二步」就能开始用**，其余按需查。

---

## 目录

1. [装环境](#1-装环境)
2. [配 API Key](#2-配-api-key)
3. [配平台账号（cookie）](#3-配平台账号cookie)
4. [第一次使用](#4-第一次使用)
5. [各功能怎么用](#5-各功能怎么用)
6. [支持哪些内容源](#6-支持哪些内容源)
7. [成本控制](#7-成本控制)
8. [常见问题](#8-常见问题)

---

## 1. 装环境

需要 Python 3.11 ~ 3.14。

### macOS / Linux

```bash
cd insight-distiller
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
```

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会自动校验 Python 版本、按有无独显选 PyTorch 版本（没独显省 2.3GB 下载）。

### 想先轻量跑通

`[all]` 会装 torch 等重依赖（约 1~2GB）。只想先试试：

```bash
.venv/bin/pip install -e ".[search]"    # 采集 + 浓缩 + 问答，够用了
```

之后按需加：

| 依赖组 | 解锁什么 | 体积 |
|--------|---------|------|
| `[search]` | 语义检索、问答 | 中（含 torch） |
| `[av]` | 音视频转写、本地录音 | 中 |
| `[ocr]` | 公众号图片型文章 | 小 |
| `[paper]` | 论文 PDF 解析 | 小 |
| `[web]` | 网页版 | 极小 |
| `[agent]` | MCP / HTTP API | 小 |

---

## 2. 配 API Key

### 2.1 生成配置文件

```bash
cp config.example.yaml config.yaml
```

> `config.yaml` 已在 `.gitignore` 里，不会被提交。

### 2.2 申请 Key

默认用 **DeepSeek**，性价比最高（浓缩一篇万字文章约 ¥0.01）。

1. 打开 https://platform.deepseek.com/
2. 注册 → 右上角「API keys」→ 新建
3. 充值（10 元够用很久）

### 2.3 填进去

**方式 A：环境变量（推荐）**

`config.yaml` 默认写的是 `${DEEPSEEK_API_KEY}`，设置环境变量即可：

```bash
# macOS / Linux，加到 ~/.zshrc 或 ~/.bashrc
export DEEPSEEK_API_KEY=sk-你的key
```

```powershell
# Windows，永久生效
setx DEEPSEEK_API_KEY "sk-你的key"
# 设完要重开终端
```

**方式 B：直接写进配置**

```yaml
providers:
  deepseek:
    api_key: sk-你的key        # 注意别把这个文件分享出去
```

### 2.4 验证

```bash
distill doctor
```

看到 `✓ deepseek` 就成了。

### 2.5 换别的模型

配置里已预置 OpenAI / Claude / Kimi / Ollama，填对应 Key 即可。也可以按任务分开指定：

```yaml
tasks:
  map:    { provider: deepseek }    # 逐块提取，量大，用便宜的
  reduce: { provider: claude }      # 汇总，要质量
  chat:   { provider: deepseek }
```

临时切换：

```bash
distill add <url> --profile quality   # 预置：local / hybrid / quality
```

**完全本地、零成本**：装 [Ollama](https://ollama.com/)，然后

```bash
ollama pull qwen2.5:14b
distill add <url> --profile local
```

---

## 3. 配平台账号（cookie）

**大多数情况不需要配。** 以下三种情况才需要：

| 现象 | 原因 |
|------|------|
| B 站视频「无可用字幕」，但网页上明显有字幕 | 字幕接口要登录态 |
| 报错 `HTTP 412/403` 或 `code=-352` | 平台风控 |
| 想抓自己的收藏夹 | 需要身份 |

### 怎么配

**不需要手动复制 cookie**，工具直接读浏览器：

```yaml
ingest:
  cookies_from_browser: chrome     # 或 edge / firefox / safari / brave
```

前提是**该浏览器已登录对应网站**。

### 注意

- macOS 上 Chrome 的 cookie 加密，首次读取会弹钥匙串授权，点「允许」。
- Safari 需要在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里给终端授权。
- 配了 cookie 后请求带着你的登录态，**别把 `data/` 目录连同 cookie 缓存分享出去**。
- 抓取频率别太高。工具已内置限流，但短时间大量请求仍可能触发风控（等几分钟自动恢复）。

---

## 4. 第一次使用

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 消化一篇文章
distill add "https://mp.weixin.qq.com/s/xxxxx"

# 看结果
distill list
distill show 1

# 建索引后就能问答
distill index
distill ask "这篇的核心方法是什么？"
```

不想每次激活环境，直接用完整路径也行：`.venv/bin/distill list`。

---

## 5. 各功能怎么用

### 5.1 消化内容

```bash
distill add <url>                    # 网页/公众号/B站/知乎/小红书/论文
distill add ~/录音.m4a                # 本地录音（自动转写，需 [av]）
distill add ~/会议.mp4                # 本地视频（抽音轨后转写）
distill add 标题 --file ~/笔记.txt     # 本地文本
distill add 标题 --paste              # 手动粘贴（反爬兜底，Ctrl-D 结束）
distill add <url> --force            # 重新处理已消化过的
distill add <url> --no-distill       # 只采集不浓缩（省钱，之后可补）
```

`add` 会按输入自动分流：论文标识 → 论文链路；音视频后缀 → 本地转写；
其他本地文件 → 文本导入；否则当 URL 采集。**不用记额外参数。**

**信息密度评分**会告诉你值不值得看原文：

- ≥ 65 分：值得读原文
- 40~65：看浓缩版就够
- < 40：疑似水文

这个分数是**规则算出来的**（信息实体密度、重复率、论证结构），不是让 LLM 主观打分，所以可复现、可比较。

### 5.2 自然语言选材

不用一个个找 URL，直接说人话：

```bash
distill plan "B站影视飓风最近3个视频"     # 只看工具怎么理解你的话，零成本
distill find "B站影视飓风最近3个视频"     # 列候选清单 + 成本预演
distill find "..." --run                # 确认后开始消化
distill find "..." --run --exclude 3,7  # 剔除第 3、7 条
```

**先 `plan` 再 `find` 是个好习惯** —— 它不发任何请求，能让你确认工具没理解错。

支持的表达：

```
B站影视飓风最近3个视频
某UP主 摄影方向 最近半年 播放量前20
知乎 关于RAG 收藏量前50
```

指令有歧义时工具会**反问**而不是瞎猜（比如没说平台）。

### 5.3 问答

```bash
distill ask "这些内容里有哪些结论互相冲突？"
distill ask "核心方法是什么" --source 12       # 只问某一篇
distill ask "..." --show-context               # 同时看检索到的原文
distill search "混合检索"                      # 只检索不问答，零成本
```

回答**强制带出处**，音视频会带时间戳。资料里没有的会明确说「知识库无依据」，不编造。

### 5.4 论文

论文用 `add` 导入，工具会自动识别是论文并走独立链路：

```bash
distill add 10.1145/3442188               # DOI
distill add https://arxiv.org/abs/2310.06825
distill add ~/paper.pdf                   # 本地 PDF
```

导入后：

```bash
distill paper similar 3                   # 找相似论文（四路召回）
distill paper similar 3 --unread          # 只看没读过的
distill paper similar 3 --add 1,3         # 直接消化推荐结果里的第 1、3 篇
distill paper lineage 3                   # 生成阅读路径（该先读哪几篇）
distill paper compare 3 7 9               # 横向对比
```

论文走独立的浓缩结构（问题 / 方法 / 实验 / 局限），不是通用摘要。

> 纯 arXiv 编号（如 `2310.06825`）目前不会被识别为论文，请用完整链接或 DOI。

### 5.5 知识关联

新内容入库会**自动**与历史内容比对，无需手动打标签：

```bash
distill graph 12          # 这篇和谁有关系
distill gaps              # 反复提及但你从未深入的概念
distill timeline "RAG"    # 某主题的认知演进
distill entity "BM25"     # 某概念在库里的分布
distill link              # 为历史内容补建关联
```

最有价值的是**结论冲突**提示 —— 两篇内容说法相反时会主动告诉你。

### 5.6 重温

```bash
distill review                    # 今天该回头看什么
distill review -g ok --id 12      # 打分：forgot / ok / easy
distill review --star 12          # 标记必看
distill review --rebuild          # 为历史内容批量判定入队
distill note 12 "我的想法"
distill note 12 -t done "补上BM25"   # 标记待办完成
distill brief                     # 周期简报（零 LLM 成本）
distill questions                 # 未解疑问追踪
```

**不是所有内容都会进重温队列** —— 只有「有待办 / 有疑问 / 被多篇关联 / 高密度」的才会。全部入队会让重温变成负担。

### 5.7 网页版

```bash
distill serve                     # http://localhost:8765
```

六个页面：内容库 / 阅读 / 问答 / 图谱 / 重温 / 简报。
阅读页**点时间戳直接跳回原视频那一秒**。

### 5.8 导出到 Obsidian

```bash
distill vault ~/Obsidian/知识库
```

关系与概念都是 `[[双链]]`，可执行项是 checkbox。这也让知识能脱离本工具存在。

### 5.9 订阅自动化

```bash
distill subscribe add finance "B站 财经UP主 最近内容" --every 24 --max 3
distill subscribe run --dry-run       # 先看会抓什么
distill subscribe run --export ~/notes
distill subscribe list
distill subscribe history finance     # 排查「为什么这条没抓到」
```

挂到 cron（macOS/Linux）或任务计划程序（Windows）即可。
注意订阅运行时**无法反问**，所以指令要先用 `plan` 确认。

### 5.10 给 AI Agent 用

```bash
distill serve-mcp                 # MCP Server，供 Claude Desktop 等接入
distill serve-agent               # HTTP API /v1
distill domain template           # 打印理财领域配置模板
```

```python
from distiller import KnowledgeBase

with KnowledgeBase() as kb:
    kb.stats("finance")                            # 先确认知识边界
    kb.search("长期国债", claim_types=["fact"])     # 只取事实
    kb.conflicts("长期国债")                        # 查分歧
```

每条返回强制带**时效、权威度、出处**，从类型层面防止 agent 把自媒体观点当客观事实。

### 5.11 运维

```bash
distill doctor                    # 环境自检，迁移后第一件事
distill cost                      # 本月花费
distill index --rebuild-fts       # 重建关键词索引（升级后可能需要）
```

---

## 6. 支持哪些内容源

| 来源 | 状态 | 说明 |
|------|------|------|
| **公众号文章** | 支持 | 图片型长图文需装 `[ocr]`；遇验证页可用 `--paste` 兜底 |
| **B 站视频** | 支持 | 优先抓字幕（快 50 倍且免费），无字幕走本地转写 |
| **通用网页** | 支持 | trafilatura 正文抽取；可配声明式规则提升质量（见 6.2） |
| **知乎** | 支持 | 回答与专栏 |
| **小红书** | 支持 | 图文笔记 |
| **论文** | 支持 | arXiv / DOI / 本地 PDF |
| **本地录音** | 支持 | mp3 / m4a / wav / flac / opus 等，本地 Whisper 转写 |
| **本地视频** | 支持 | mp4 / mkv / mov 等，自动抽音轨 |
| **本地文本** | 支持 | `--file`，需 UTF-8 |
| **手动粘贴** | 支持 | `--paste`，任何反爬场景的兜底 |
| 播客 RSS | 未实现 | 见 `TODO.md` #3 |
| YouTube | 部分 | 深链跳转已支持，采集器未专门适配 |
| 抖音 | 不支持 | 签名+设备指纹难度高，且短视频压缩无意义。见 `TODO.md` #17 的替代方案 |

### 6.1 平台开关与参数

各平台风控强度差异很大，可以按平台单独配：

```yaml
ingest:
  cookies_from_browser: chrome     # 全局默认
  platforms:
    xhs:
      enabled: true
      rate_limit_sec: 3.0          # 小红书风控最严，连续请求易触发验证
    bilibili:
      cookies_from_browser: edge   # 单独覆盖全局
      timeout: 90
    zhihu:
      enabled: false               # 不想抓知乎就关掉
```

```bash
distill platforms                  # 看各平台状态
distill platforms --test <url>     # 测某链接会走哪个采集器
```

> 禁用某平台后，该平台链接会**明确报错**而不是降级到通用抽取。
> 因为小红书正文在嵌套 JSON、B 站正文在字幕接口，通用抽取只会拿到
> 空内容或噪声——给你残缺数据却不说原因，比直接报错危险。

### 6.2 给常读的站点配规则（不用写代码）

通用抽取对结构特殊的页面（带侧栏的文档站、评论区很长的博客）
有时会抽进噪声。常读的站点可以配选择器：

```yaml
ingest:
  sites:
    - name: ruanyifeng
      match: ["ruanyifeng.com"]
      title: "h1#page-title"
      content: "div#main-content"
      author: null
      remove: ["div.comments", "nav", "footer"]   # 先删噪声再抽正文
```

选择器用 CSS；以 `/` 开头则按 XPath 处理（少数情况如按文本定位只有 XPath 能表达）。

**适用边界**：只能处理「正文在固定 DOM 节点」的站点。
需要请求签名（B 站字幕、抖音）、内容在嵌套 JSON 且路径会变（小红书）、
纯前端渲染的站点，**配选择器无效**——声明式解决「解析」，解决不了「拿到」。

配完可以直接验证：

```bash
distill platforms --test https://ruanyifeng.com/blog/xxx
```

选择器失效时（站点改版）会明确报错并指出是哪个选择器，不会静默抽到空内容。

### 6.3 抓某个 UP 主的系列

工具能识别「系列」——即**标题必含词**，与「主题」（语义方向）不同：

```bash
# 三种写法都认
distill find "B站UP主海安雨的每天一个宝藏问题系列 最新12个" --run
distill find "B站UP主海安雨【每天一个宝藏问题】前30" --run
distill find "B站UP主某某 XX合集 播放量前20" --run
```

> 需要登录态：B 站投稿列表接口对未登录请求返回 `-352`。
> 在 `config.yaml` 设 `ingest.cookies_from_browser: chrome`，
> 然后用 `distill doctor` 确认「浏览器Cookie」那行显示「含登录态」。

**抓整个系列**（长寿系列可能有 200+ 期）：

```bash
distill find "B站UP主海安雨的每天一个宝藏问题系列 前250" --run --subtitle-only
```

数量给足即可，工具会翻页并本地按标题过滤
（B 站的 `keyword` 参数是模糊匹配，几乎不起过滤作用，所以真正生效的是本地过滤）。

**`--subtitle-only` 在批量场景下几乎是必加的**：无字幕视频会启动本地 Whisper，
单条占用几分钟 CPU（实测 482% 占用），200 条里混几条就能把任务拖住。
加上它则直接跳过，并单独统计「跳过 N 条（无字幕）」，不与失败混在一起。

无 LLM 时可只入库不浓缩：

```bash
distill find "..." --run --subtitle-only --no-distill
```

批量过程中会显示进度与剩余时间估算，跑完给出失败清单：

```
[47/211]  剩余约 18 分 20 秒
  188. 为什么RoPE可以外推？【每天一个宝藏问题】

✓ 完成：成功 205 条，跳过 4 条（无字幕），失败 2 条 · 用时 24 分 51 秒
```

**中途 Ctrl-C 是安全的**——已完成的都已入库，重跑同一条指令会自动跳过
已消化的，等于续跑。失败的也一样，重跑只会重试那些。

> 实测 211 期全部命中 AI 自动字幕，零转写、零 LLM 成本，约 25 分钟跑完
> （耗时主要是平台限流）。

### 6.4 导出 Q&A 合集

问答体系列（「每天一个宝藏问题」「XX 十问」「面试题精讲」）
标题即问题、正文即答案，适合导成速查文档：

```bash
distill qa-export out.md --series "每天一个宝藏问题"   # 默认不限量
distill qa-export out.md --ids 2-13 --title "我的 LLM 问题集"
distill qa-export --author 海安雨          # 省略路径则打印到终端
```

标题本身是问句时（「为什么X？」「如何Y？」）会走**问答 schema**，产出：

```markdown
## Q12. 234. 为什么 MLA 连 V 也不需要显式解压？

**答：因为 W_UV 可以吸收进输出投影 W_O，等价变换后 V 从未被物化。**

<sub>前置：低秩分解、矩阵乘法结合律、KV Cache 基本原理</sub>

- MLA 把 KV 压成低秩的 C_KV，解压时需要 C_KV·W_UK 得 K、C_KV·W_UV 得 V。
- 注意力输出是 A·V·W_O，代入后变成 A·(C_KV·W_UV)·W_O。
- 矩阵乘法可结合，先算 W_UV·W_O 融合成一个矩阵，V 就不必单独算出来。

> ⚠ 容易误以为「不解压」是指不计算，其实是计算被重排到了 W_O 里。

**引申**

- **K 为什么不能同样吸收？** — K 要参与 RoPE 旋转，位置相关无法预先融合。
- **省了多少显存？** — DeepSeek-V2 报告约为 MHA 的 1/14。

<sub>[原片](…) · #2 · 3分23秒</sub>
```

阅读路径是设计好的：**扫「答」判断懂没懂 → 不懂看要点 → 想深入看引申**。

超过 30 题时目录按主题自动分组（位置编码 / 注意力 / KV Cache / MoE /
强化学习 / 剪枝量化 / 微调 / 算子加速 …），方便按兴趣跳读。

> ⚠ **这个形态需要 LLM**。未配置时会退化为「原文节选」——
> 从字幕里挑信息密度高的句子。节选**无法**给出一句话简答、引申问题，
> 也无法纠正语音转写的术语错误（「库达」=CUDA、「西塔」=θ）。
> 文档顶部会明确标注是哪种模式。

### 6.5 先采集、后浓缩

没配 LLM 时可以先把正文批量入库，之后再补浓缩——
不必重新采集（平台限流、字幕接口可能失效、无字幕的还要重跑转写）：

```bash
# ① 先采集（零成本）
distill find "B站UP主某某的XX系列 前250" --run --subtitle-only --no-distill

# ② 配好 LLM 后补浓缩，正文复用库内
distill distill-pending --series "XX系列"

# ③ 导出
distill qa-export cases/out.md --series "XX系列"
```

实测成本参考：211 期 × 平均 857 字，DeepSeek chat 全系列约 **$0.126（0.9 元）**。
`distill-pending` 开始前会显示本月已用额度，结束时报告本次花费。

> 导出到 `cases/` 目录下，产物不会进版本库也不会被打包（见 `cases/README.md`）。

> 未配置 LLM 时会退化为**原文节选**（从字幕里挑信息密度高的句子），
> 并在文档顶部明确标注。配好 LLM 后重跑
> `distill add --force` 再导出即可得到模型生成的摘要。

**录音文件的实际体验**：1 小时录音在 M 系列芯片上约 15~25 分钟转写完（CPU），有 NVIDIA 显卡约 3~6 分钟。首次会下载 Whisper 模型（large-v3 约 1.5GB）。

嫌慢可以降档，但中文场景不太建议：

```yaml
transcribe:
  model: medium        # large-v3 → medium，快约 2 倍，但中文错字明显增多
```

---

## 7. 成本控制

```yaml
limits:
  monthly_budget_usd: 10      # 超了直接拒绝调用，硬护栏
  max_items_per_run: 20
```

几个省钱习惯：

1. **先 `plan` 再 `find`** —— `plan` 零成本，能避免抓错一批内容。
2. **`find` 默认只列候选**，要加 `--run` 才真的花钱。
3. **`search` 和 `brief` 完全不调 LLM** —— 检索和简报都是零成本。
4. **低密度内容自动跳过精读** —— 水文不浪费 token。
5. **转写和采集结果永久缓存** —— prompt 改了只重跑浓缩，不重复转写。

看花了多少：

```bash
distill cost
```

---

## 8. 常见问题

**`distill` 命令找不到**
环境没激活。`source .venv/bin/activate`（Windows: `.venv\Scripts\activate`），或用完整路径。

**B 站视频显示「无可用字幕」**
字幕接口要登录态。在 `config.yaml` 配 `ingest.cookies_from_browser: chrome` 并确保浏览器已登录。

**报错 `HTTP 412` 或 `code=-352`**
平台风控。配 cookie 通常能解决；已配还失败就是请求太频繁了，等几分钟。

**公众号返回「环境异常」验证页**
换个网络环境或稍后再试，也可以直接：

```bash
distill add 文章标题 --paste
# 然后把正文粘进来，Ctrl-D 结束
```

**问答说「知识库中没有相关依据」**
这是**正确行为**——它不会编。检查一下：

```bash
distill list                  # 内容真的入库了吗
distill index                 # 索引建了吗
distill search "关键词"        # 检索能命中吗
```

**某个关键词怎么都搜不到**
如果是从旧版本升级上来的，重建一次关键词索引：

```bash
distill index --rebuild-fts
```

**中文显示成乱码（Windows）**
换 Windows Terminal 或 PowerShell 7，或执行 `chcp 65001`。这只影响终端显示，**存进库里的始终是 UTF-8**。

**想备份 / 换机器**

```bash
python scripts/package.py --with-data
```

或者手动拷 `data/` 目录和 `config.yaml` 就够了——库内只存相对路径，拷到哪都能用。

**首次检索/转写卡住不动**
在下模型（embedding 约 100MB，Whisper large-v3 约 1.5GB）。国内网络可以配镜像：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

---

## 附：完整命令清单

```bash
distill --help                    # 看全部命令
distill <命令> --help             # 看某命令的全部选项
```

| 类别 | 命令 |
|------|------|
| 消化 | `add` `find` `plan` |
| 查看 | `list` `show` `search` `cost` |
| 问答 | `ask` `index` |
| 论文 | `paper similar/lineage/compare`（导入用 `add`） |
| 知识 | `graph` `gaps` `entity` `timeline` `claims` `link` |
| 重温 | `review` `note` `brief` `questions` `vault` |
| 服务 | `serve` `serve-agent` `serve-mcp` |
| 订阅 | `subscribe add/list/run/toggle/remove/history` |
| 运维 | `doctor` `domain` `platforms` |
| 导出 | `vault` `qa-export` |

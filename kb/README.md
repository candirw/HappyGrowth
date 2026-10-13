# kb/ · 迷你图书馆（Part 1 真相源）

> 唯一真相源。所有知识都是纯 Markdown，git 管理，可被 `../engine/insight-distiller` 索引与检索。
> 完整设计见 `../docs/03_架构.md` §4，这里是**可直接照抄的落地版**。

## 目录

```
kb/
├── knowledge/
│   ├── ideas/        灵感：一句话~一段，长生命周期
│   ├── docs/          自撰文档：结构化，是产出物
│   ├── frontier/      前沿方向：外来内容，短时效
│   └── inbox/         ★ 一切新东西先落这里，零门槛
└── ATTACHMENTS/        图片等非文本附件
```

## 三类内容的 frontmatter（新建文件时复制对应模板）

### `knowledge/ideas/*.md`

```yaml
---
type: idea
author:                         # 必填，谁写的
created: 2026-10-13
status: raw                     # raw | exploring | doing | done | dropped
tags: []
related: []                     # 由系统填，不手填
source: []                      # 触发它的外部内容（可溯源）
---

正文：一句话到一段，想到什么写什么。
```

### `knowledge/docs/*.md`

```yaml
---
type: doc
author:
created: 2026-10-13
updated: 2026-10-13             # 每次改动更新
status: draft                   # draft | stable | deprecated
supersedes: null                # 取代了哪篇（文件名），防多版本并存
tags: []
---
```

### `knowledge/frontier/*.md`

```yaml
---
type: frontier
author:                         # 谁发现的
created: 2026-10-13
source_url:
expires: 2027-01-13             # ★ 必填，到期自动归档
verdict: null                   # 转化成自己的判断了吗，90 天为空会被提醒
tags: []
---
```

## 使用规则（不是建议，是纪律）

| 规则 | 原文见 |
|---|---|
| 新东西一律先进 `inbox/YYYY-MM-DD.md`，追加写，不建新文件 | `../docs/03_架构.md` §4.4 |
| 每周复盘三问后才分类进三大目录：这周用过吗 / 现有工具能覆盖吗 / 是一次性任务吗 | 同上 |
| **只有作者能改写原内容，其他人只能追加批注** | `../docs/03_架构.md` §4.3 |
| 推翻别人的结论不删除，加冲突说明，让分歧本身成为有价值的信息 | 同上 |
| `frontier/` 的 `expires` 到期自动归档，`verdict` 空置超 90 天在周报提示 | 同上 |

## 接入检索

本目录本身不含任何索引文件（SQLite/向量都是派生物）。把 `kb/` 接进 `engine/insight-distiller` 的摄入链路后，即可用 `distill ask` / `distill search` / MCP 工具检索。接入脚本见 C1 阶段任务（`../PROGRESS.md`）。

## 关于这是公开仓库的提醒

本仓库（HappyGrowth）当前是 **Public**。在往 `ideas/` / `docs/` / `inbox/` 写真实内容前，请确认：
- 这些内容是否适合公开
- 如果不适合，考虑把仓库切成 Private，或者把 `kb/` 拆成独立的私有仓库（`../docs/03_架构.md` 原方案就是建议独立仓库）

见 `../DECISIONS.md` ADR-011。

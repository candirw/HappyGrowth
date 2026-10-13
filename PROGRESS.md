# PROGRESS · 当前进度

状态：**规划中（规划已完成，尚未开工）**

> 本文件是 AI 每次接手本项目前应先读的文件。更新时机：每次开工/收工、每完成一个 Checkpoint。

---

## 当前做到哪

- [x] 会议纪要初步分析（已废弃框架：五大模块并列）
- [x] 用户两轮订正吸收：P1（复用优先）、P2（知识管理为核心基建）
- [x] 需求对齐（`docs/01_需求对齐.md`）：拆清 5 个 Part 的本质问题与难点
- [x] 复用清单（`docs/02_复用清单.md`）：盘点 `insight-distiller` 已覆盖约 80% 的 Part 2 能力，并核对 MCP 工具列表
- [x] 架构设计（`docs/03_架构.md`）：4 条硬约束 + 目录约定 + 三层检索
- [x] 各模块设计（`docs/04_各模块设计.md`）
- [x] Roadmap 与风险（`docs/05_Roadmap与风险.md`）
- [x] 正式 PLAN / PRD / PROGRESS / DECISIONS 整理完毕
- [x] git 仓库初始化 + 推送到 GitHub（`candirw/HappyGrowth`，SSH 认证已配置）
- [x] **目录分区落地**：`engine/`（复用引擎）/ `kb/`（真相源）/ `projects/`（代码产物）/ `plans/`（计划复盘）/ `data/`（原始输入），各含说明 README
- [x] `insight-distiller` 迁入 `engine/insight-distiller/`（已排除 `.venv`/`data/`/`cases/`/真实 `config.yaml`，1.8G → 1.5M）
- [x] `projects/_template/` 脚手架（README/PLAN/PROGRESS/DECISIONS/.gitignore）
- [x] `CONTRIBUTING.md`：加协作者的网页步骤 + 新成员本地 SSH key 配置 + 写入规矩
- [ ] 确认 B1（手环能否自动导出）
- [ ] 确认 B2（仓库 Public/Private，**已因"要加新成员"从待定升级为阻塞**，见 ADR-012）
- [ ] 把 `kb/` 接入 `engine/insight-distiller` 的摄入链路（C1 剩余工作）
- [ ] 决定是否把 `策略研究档案/` 等现有项目迁入 `projects/`（需先过一遍是否含机密信息）

## 下一步

1. **用户确认 B2**（仓库可见性）——**加第一个真实协作者前必须先定**
2. 用户确认 B1（手环数据导出方式）
3. 把 `kb/` 目录接进 `engine/insight-distiller` 的摄入链路，跑通一次真实入库+关联提示
4. 若决定迁入现有项目（`策略研究档案/` 等），先逐一过一遍是否含工作机密信息再决定公开/私有

## 已知坑 / 待确认

| # | 内容 | 影响 |
|---|---|---|
| **B2** | 仓库 Public/Private 怎么选（ADR-011/012） | **最高优先级**——加协作者前必须定，否则真人身份+真实内容一起暴露 |
| B1 | 手环型号 + 能否自动导出 CSV/API（还是只能截图） | 决定 Part 5 能否"零人工"运行 |
| Q1 | "一小群人"具体几人、信任边界（公开/私密/部分可见） | Part 1 权限设计 |
| Q3 | 团队里谁写代码、人数 | Part 3 协作模型 |
| Q4 | AI 文娱产出是自用还是对外分发 | 是否涉及版权合规 |
| Q6 | 已有 `策略研究档案/` 等现有项目是否迁入 `projects/` | 迁移成本 + 是否涉及机密信息，当前未自动迁入 |

## Checkpoint 进度

| Checkpoint | 状态 |
|---|---|
| C1 地基 | **进行中**：目录骨架 + engine 已就绪，待接入检索链路 + 确认 B2 |
| C2 计划闭环 | 未开始（依赖 B1） |
| C3 项目模板 | **部分完成**：`projects/_template/` 已就绪 |
| C4 核查能力 | 未开始 |
| C5 外部供料 | 未开始 |
| C6 文娱应用 | 未开始 |
| C7 成本治理 | 未开始 |

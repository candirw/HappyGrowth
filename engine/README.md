# engine/ · 复用的知识管理引擎

> 本目录只放**复用的现成引擎**，不放自己写的业务代码（自己的项目放 `../projects/`）。

## 当前内容

| 目录 | 是什么 | 对应 PLAN.md 里的角色 |
|---|---|---|
| `insight-distiller/` | 知识采集/浓缩/检索/关联/MCP 出口的完整实现（M1–M6） | `kb/` 的索引层 + Part 1/2/4 的检索与关联能力来源 |

## 重要：这里不含运行时数据和密钥

迁入时已排除：`.venv/`、`data/`（真实知识库数据库）、`cases/`、`__pycache__/`、`config.yaml`（真实密钥配置，只保留 `config.example.yaml`）。

`engine/insight-distiller/.gitignore` 已随代码一并带入，本地跑起来产生的 `data/`、`.venv/` 等不会被意外提交。

## 本地如何跑起来

```bash
cd engine/insight-distiller
python -m venv .venv && .venv/bin/pip install -e ".[all]"
cp config.example.yaml config.yaml      # 本地改，这个文件不会被提交
export DEEPSEEK_API_KEY=sk-xxx
.venv/bin/distill doctor
```

详见 `insight-distiller/README.md` 与 `MANUAL.md`。

## 为什么放这里而不是 `projects/`

`projects/` 是 Part 3（点子到代码）的产物区，每个子目录是一个独立探索的项目。
`insight-distiller` 不是"一个点子"，是**支撑整个仓库运转的基础设施**——`kb/` 里的知识要靠它检索、Part 4 的核查要靠它取证、未来的 MCP 出口也是它提供的。所以单独归为 `engine/`，与"产物"区分开。

见 `../DECISIONS.md` ADR-010。

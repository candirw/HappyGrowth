# CONTRIBUTING · 新成员加入指南

> 给"一小群人"用的，不是开源项目标准模板。加一个人大约 10 分钟。

---

## 第一步：仓库所有者在 GitHub 网页加协作者

1. 打开仓库 → `Settings` → 左侧 `Collaborators and teams` → `Add people`
2. 填对方的 GitHub 用户名或邮箱，选权限级别，发送邀请
3. 对方接受邀请后生效

### 权限级别怎么选

| 级别 | 能做什么 | 建议 |
|---|---|---|
| Read | 只能看 | 不必单独给——仓库是 Public，本来就能看 |
| **Write** | 能直接 push / 开 PR / 管理 Issue | **小团队默认给这个**，不需要走审批流程 |
| Maintain / Admin | 管理仓库设置、能删仓库 | 只给仓库所有者 |

> **仓库当前可见性决定了"加协作者"的真正意义**：Public 仓库任何人不用被邀请就能 clone / fork / 提 PR；协作者权限给的是**直接 push 到 main 的能力**，不是"能不能看"。见 `DECISIONS.md` ADR-011、ADR-012。

## 第二步：新成员本地配 SSH key（每人在自己电脑上各做一次）

```bash
ssh-keygen -t ed25519 -C "你的名字-github" -f ~/.ssh/id_ed25519_github -N ""
cat ~/.ssh/id_ed25519_github.pub
```

把输出的**公钥**（一整行 `ssh-ed25519 ...`）贴到**自己账号**的 `https://github.com/settings/ssh/new`。

在自己电脑的 `~/.ssh/config` 里加（没有这个文件就新建）：

```
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
```

验证：

```bash
ssh -T git@github.com
# 看到 "Hi <你的用户名>! You've successfully authenticated" 就对了
```

然后正常 clone：

```bash
git clone git@github.com:candirw/HappyGrowth.git
```

## 第三步：了解这里的写入规矩（不是技术问题，是约定）

这不是普通代码仓库，`kb/` 下是一小群人的真实灵感/文档/发现。写入前先读 `kb/README.md`，核心就两条：

| 规矩 | 为什么 |
|---|---|
| **新东西先落 `kb/knowledge/inbox/`**，不要直接建新文件到 `ideas/`/`docs/`/`frontier/` | 零门槛写入，分类是之后的事（见 `docs/03_架构.md` §4.4） |
| **只有作者本人能改自己写的内容，别人只能追加批注** | 防止"被优化"的顾虑，让人敢写；推翻别人结论不删除，加冲突说明（见 `docs/03_架构.md` §4.3） |

写东西时，frontmatter 的 `author` 字段填你的 GitHub 用户名（或约定好的称呼），这个字段是必填的（见 `PRD.md` Part 1 的 FR-1.1 附近约定）。

## 加人前请先确认（仓库所有者的责任）

- [ ] 已经和新成员对齐过 **B2**（仓库 Public/Private 的决定）——新成员的身份和写入内容会跟着可见性走
- [ ] 新成员知道 `kb/` 里可能出现别人的真实想法，遵守"只追加不改写"
- [ ] 给的权限级别是 Write，不是 Admin（除非 ta 也要共同维护仓库设置）

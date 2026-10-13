# 可移植性与可迭代性设计

> 两个长期约束：
> ① **跨平台**：当前 macOS 开发，后续要迁移到 **Windows**
> ② **可迭代**：功能会持续增加（M1→M6），不能每次都推翻重写
>
> 原则：**平台差异必须收敛到少数几个适配点，而不是散落在业务代码里。**

---

## 1. 跨平台风险清单（已逐条落实到设计）

下面每一条都是实际会导致「macOS 跑得通、Windows 一跑就炸」的坑。

### 1.1 致命级：Windows 默认编码不是 UTF-8

Python 在 Windows 上 `open()` 的默认编码是系统 ANSI（简体中文环境为 `cp936/GBK`）。
你的内容全是中文，一旦漏写编码，**读写中文必然乱码或抛 `UnicodeDecodeError`**。

**落实**：
- 全项目**禁止裸 `open()`**，统一走 `store/fsio.py` 的封装，内部强制 `encoding="utf-8"`。
- 数据库连接、JSON 序列化、subprocess 输出捕获全部显式指定 UTF-8。
- CI 里加一条静态检查，扫描裸 `open(` 直接失败。

### 1.2 致命级：macOS 原生 OCR 在 Windows 不存在

原方案 P0 的公众号图片 OCR 选了 `ocrmac`（Apple Vision），**Windows 上完全不可用**。

**落实**：抽象为 `OCRProvider` 接口，按平台自动选择：

| 平台 | 实现 | 说明 |
|------|------|------|
| macOS | `ocrmac`（Apple Vision） | 无需下模型，中文准确率高 |
| Windows | `RapidOCR`（ONNXRuntime） | 纯 pip 安装、跨平台、中文强，无需 Docker |
| 兜底 | `RapidOCR` | 两端都可用，作为统一降级路径 |

**关键决定**：`RapidOCR` 在 macOS 上也能跑。因此**默认就用 RapidOCR 保证一致性**，`ocrmac` 仅作为 macOS 上的可选加速项（`ocr.engine: auto|rapid|ocrmac`）。
这样 Windows 迁移时 OCR 行为**完全一致**，不会出现"换平台后摘要质量变了"这种极难排查的问题。

### 1.3 高危：外部二进制（ffmpeg）

macOS 靠 brew、Windows 靠 winget/scoop，路径与安装方式都不同；本机连 brew 都没有。

**落实**：
- **`ffmpeg` 走 `imageio-ffmpeg` Python 包**，它自带各平台预编译二进制，`pip install` 即得，**两端零系统依赖**。
- 仍支持系统 ffmpeg 优先（`binaries.ffmpeg` 可显式配置路径），检测顺序：配置 → PATH → imageio-ffmpeg 自带。
- 所有外部进程调用集中在 `platform/binaries.py`，用**列表参数**调用（绝不 `shell=True`，Windows 上路径含空格必炸且有注入风险）。
- M1 不需要 ffmpeg（无音视频转写），列为**可选依赖组** `[av]`，装不上不影响 M1 可用。

### 1.4 高危：ASR 后端差异

`mlx-whisper` 是 Apple Silicon 专属；Windows 上要用 CUDA 或 CPU。

**落实**：`ASREngine` 抽象，`faster-whisper`（基于 CTranslate2，**三平台通用**）作为默认与基准实现；`mlx-whisper` 仅作 macOS 可选加速。设备选择自动探测：`cuda → mps/coreml → cpu`。
**基准实现必须是跨平台的那个**，加速实现只能是可选项——否则迁移时等于重写。

### 1.5 高危：路径与文件名

| 坑 | 后果 | 落实 |
|----|------|------|
| 硬编码 `/` 分隔符 | Windows 路径错误 | 全程 `pathlib.Path`，禁止字符串拼路径 |
| 文件名含 `\ / : * ? " < > \|` | Windows 无法创建文件 | 用标题做文件名前必须过 `safe_filename()` 清洗 |
| Windows 保留名 `CON/PRN/AUX/NUL/COM1-9/LPT1-9` | 创建失败 | 清洗函数一并拦截并加后缀 |
| Windows 路径 260 字符上限 | 长标题+深目录报错 | 文件名截断至 80 字符 + 短哈希保唯一 |
| 大小写敏感性不同（macOS 默认不敏感、Linux 敏感） | 迁移后找不到文件 | 内部标识符统一小写；不依赖仅靠大小写区分的文件名 |
| 用户目录位置不同 | 数据目录写错地方 | 用 `platformdirs` 取标准数据目录 |

数据目录策略：
- 默认**项目内 `./data`**（便于整体拷走、便于备份，对迁移最友好）。
- 可切换为系统标准目录（macOS `~/Library/Application Support/insight-distiller`、Windows `%LOCALAPPDATA%\insight-distiller`），由 `platformdirs` 提供。
- **数据库内一律不存绝对路径**，只存相对 `data/` 的相对路径 → **整个文件夹拷到 Windows 直接能用**，这是迁移的关键设计。

### 1.6 中危：其他

| 项 | 落实 |
|----|------|
| 换行符 CRLF/LF | 仓库加 `.gitattributes` 统一 LF；写文本用 `newline="\n"` |
| SQLite 并发（Windows 文件锁更严格） | 开 WAL 模式 + `busy_timeout`；连接不跨线程复用 |
| 终端不支持彩色/Emoji（旧版 cmd） | 用 `rich`，自动降级；`--plain` 开关输出纯文本 |
| 浏览器 cookie 路径不同 | 交给 `browser-cookie3`/`yt-dlp` 处理，不自己拼路径 |
| 时区与本地化 | 全库时间统一 **UTC 存储**，展示时转本地 |
| 长路径与中文路径 | 测试用例专门覆盖含中文与空格的路径 |

---

## 2. 平台适配层（唯一允许出现平台判断的地方）

```
src/distiller/platform/
├── paths.py        # 数据目录、相对路径解析、safe_filename
├── fsio.py         # 强制 UTF-8 的读写封装
├── binaries.py     # 外部二进制探测（ffmpeg 等）+ 安全 subprocess
├── device.py       # 计算设备探测（cuda / mps / cpu）
└── caps.py         # 平台能力矩阵（哪些 provider 当前可用）
```

**硬规则**：业务代码里**不允许出现 `sys.platform` / `platform.system()` 判断**。
CI 加静态检查，违反即失败。这样迁 Windows 时只需检查这一个目录。

`distill doctor` 会输出平台能力矩阵，迁移后第一件事就是跑它：

```
$ distill doctor
平台      : Windows 11 (AMD64) · Python 3.12.4
数据目录  : .\data  (可写 ✓)
编码      : UTF-8 强制 ✓
OCR       : RapidOCR ✓   (ocrmac 不适用于本平台，已跳过)
ffmpeg    : imageio-ffmpeg 自带 ✓
ASR       : faster-whisper ✓  设备: cuda
LLM       : deepseek ✓ (API 可达)   ollama ✗ (未运行，降级链已就绪)
数据库    : WAL 已启用 ✓  schema v1 ✓
⚠ 1 项提示：未检测到 Ollama，profile=local 不可用，将使用 deepseek
```

---

## 3. 可迭代性设计

### 3.1 插件注册表（新增来源不改主流程）

采集器、发现器、LLM provider、OCR、ASR **全部走注册表 + 抽象基类**：

```python
@register_adapter("bilibili")
class BilibiliAdapter(SourceAdapter):
    def match(self, url: str) -> bool: ...
    def fetch(self, url: str) -> RawContent: ...
```

新增一个平台 = 新增一个文件 + 一个装饰器，**不碰 pipeline**。

### 3.2 schema 版本化与迁移

- `meta` 表存 `schema_version`；`store/migrations/` 下按版本号存 SQL。
- 启动时自动检测并升级，升级前自动备份数据库文件。
- M1 就把 M6 要用的表（`claims`/`entities`/`relations`/`questions`/`reviews`）一次建好，**但迁移机制仍必须有**——后续一定会加字段。

### 3.3 Prompt 与浓缩结果版本化

- prompt 存独立文件并带版本号，`digests.schema_version` + `digests.prompt_version` 记录产出来源。
- 好处：prompt 迭代后可只重跑摘要（**转写与采集结果永久缓存，不重复花钱**），且能对比新旧质量。

### 3.4 分层解耦（每层可独立替换）

`ingest → transcribe → normalize → distill → index → link` 各层只依赖数据模型，不互相引用实现。
换向量库、换 ASR、换 LLM 都只动一层。

### 3.5 测试与 CI

- 纯函数（分块、密度评分、文件名清洗、Query IR 解析）做单元测试，不依赖网络。
- 网络与 LLM 调用用 fixture 录制回放，保证离线可测。
- **GitHub Actions matrix：`macos-latest` + `windows-latest` 双平台跑测试**，从第一天就防止平台回归——这是跨平台承诺唯一可靠的保障方式。

---

## 4. 迁移到 Windows 的实际步骤（已实测验证）

### 4.1 在 macOS 上打包

```bash
python scripts/package.py                 # 只打代码，约 400KB
python scripts/package.py --with-data     # 带知识库（推荐）
python scripts/package.py --with-models   # 连 embedding 模型缓存一起带
python scripts/package.py --dry-run       # 先看清单
```

**为什么要脚本而不是手动 zip** —— 有几样东西拷过去会造成难排查的故障，
而它们恰好都在项目目录里：

| 必须排除 | 后果 |
|----------|------|
| `.venv/` | 里面是 macOS 二进制与写死的绝对路径，拷过去 pip/python 全部失效 |
| `config.yaml` | **含 API Key**，不该进传输包 |
| `*.egg-info/` | 构建残留，内含本机绝对路径 |
| `__pycache__/` | macOS 编译的 .pyc |
| `data/audio/` | 音频缓存动辄几 GB，而转写结果已在库里，重下成本远低于传输 |

`data/distiller.db` 反而应该带上——**库内只存相对路径**（§1.5 的设计决定），
拷过去直接可用，历史消化结果零转换。WAL 的 `-wal`/`-shm` 旁文件也一并带走，
它们可能含未落盘事务。

### 4.2 在 Windows 上安装

```powershell
# 1. 装 Python 3.11 ~ 3.14
winget install Python.Python.3.12

# 2. 解压后进入目录，跑安装脚本
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 3. 填 config.yaml 里的 API Key

# 4. 验证
.venv\Scripts\activate
distill doctor
distill list        # 历史数据应完整可读
distill index --rebuild   # 向量索引需重建（不随包传输）
```

脚本会自动处理三件容易出错的事：

1. **Python 版本校验** —— 低于 3.11 或高于 3.14 直接报错并给安装指引，
   而不是让 pip 抛出难懂的依赖错误。
2. **误拷的 `.venv` 检测** —— 若目录里有非 Windows 的 venv（手动 zip 常见），
   删除重建而非在上面继续装。
3. **PyTorch 版本选择** —— 检测有无 NVIDIA 显卡。没有独显时装 CPU 版
   （约 200MB），有则装 CUDA 版（约 2.5GB）。**不做这个判断会让绝大多数
   用户白下 2.3GB**，而且 CPU 版除转写慢一些外功能完全一致。

### 4.3 实测结论

在干净目录解压后用 Python 3.14 全新安装，结果：

- `pip install -e ".[all]"` 成功（含 torch / sentence-transformers / mcp 等 100 余个包）
- `distill doctor` 全绿
- **339 项测试全部通过**

这验证了包是自洽的——`package-data`（schema.sql、migrations、
web 模板与静态文件）都正确包含在内。

**目标：除装 Python 外零额外系统依赖，数据零转换。已达成。**

---

## 4.5 打包过程中发现并修复的两个真实缺陷

这两个都是「文档承诺了但实现没跟上」，只有真的走一遍全新安装才会暴露。

### `all` extra 不存在

本文件 §4 一直写着 `pip install -e ".[all]"`，但 `pyproject.toml` 里
**从未定义过 `all`**。更糟的是 **pip 对不存在的 extra 只给 warning 不报错**：

```
WARNING: insight-distiller 0.1.0 does not provide the extra 'all'
```

用户会以为装全了，实际只装了核心依赖，然后 `distill serve` 报缺依赖——
而错误信息完全指不到真正原因。已补上真实的 `all` 组。

### OCR 依赖在 Python 3.13+ 上无法安装

`rapidocr-onnxruntime` **所有版本都要求 `python_version < 3.13`**，
而 python.org 现在默认下载的就是 3.13/3.14。照文档装必然失败：

```
ERROR: Could not find a version that satisfies the requirement
       rapidocr-onnxruntime>=1.3
```

根因是 RapidOCR 在 2.x 改了包名（`rapidocr-onnxruntime` → `rapidocr`），
且 **API 也变了**：

| 包 | Python | 调用与返回 |
|----|--------|-----------|
| `rapidocr-onnxruntime` 1.x | 仅 <3.13 | 吃 `BytesIO`，返回 `(list[[box,text,score]], elapse)` |
| `rapidocr` 3.x | 3.13/3.14 可用 | 直接吃 `bytes`，返回对象，文本在 `.txts` |

处理方式是**双包兼容**而非直接切新包：依赖用环境标记按 Python 版本选，
代码里 try-import 并适配两种返回格式。这样老环境不用动，新机器也能装。

**教训**：跨平台承诺不能只靠「依赖都是纯 Python / 有预编译 wheel」这种
静态判断——依赖自身的 Python 版本支持窗口会随时间关闭。定期在目标
Python 版本上跑一次全新安装，是唯一可靠的验证方式。

---

## 5. 因跨平台而修订的选型（相对原方案）

| 原选型 | 修订为 | 原因 |
|--------|--------|------|
| OCR 用 `ocrmac` | **默认 `RapidOCR`**，`ocrmac` 降为 macOS 可选加速 | Windows 无 Apple Vision；且保证两端 OCR 结果一致 |
| ffmpeg 靠 brew 安装 | **`imageio-ffmpeg` 自带二进制** | 本机无 brew；Windows 安装方式不同；免系统依赖 |
| 依赖管理用 `uv` | **标准 `venv` + `pip`**（`pyproject.toml` 仍兼容 uv） | uv 需额外安装；venv/pip 是 Python 标准，两端行为一致 |
| 数据目录随意放 | **项目内 `./data` + 库内只存相对路径** | 整个文件夹可直接拷贝迁移 |
| 未考虑编码 | **强制 UTF-8 IO 封装 + 静态检查** | Windows 默认 GBK，中文内容必炸 |

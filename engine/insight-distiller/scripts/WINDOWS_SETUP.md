# Windows 安装说明

解压后照这个做，全程约 10 分钟（主要是等依赖下载）。

---

## 快速开始

1. **装 Python 3.11 ~ 3.14**（如果还没有）

   ```powershell
   winget install Python.Python.3.12
   ```

   或从 [python.org](https://www.python.org/downloads/) 下载。
   安装时务必勾选 **"Add python.exe to PATH"**，否则后面找不到 python。
   装完**重开 PowerShell**。

2. **在项目目录里运行安装脚本**

   ```powershell
   cd <解压出来的 insight-distiller 目录>
   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
   ```

   > 为什么要 `-ExecutionPolicy Bypass`：Windows 默认禁止运行未签名脚本。
   > 这个参数只对当次调用生效，不改系统设置。

3. **填 API Key**

   打开 `config.yaml`，找到 `providers.deepseek.api_key` 填上。
   [DeepSeek 申请地址](https://platform.deepseek.com/)（也可改用 OpenAI / Kimi / 本地 Ollama）。

4. **验证**

   ```powershell
   .venv\Scripts\activate
   distill doctor
   ```

---

## 没有 NVIDIA 显卡的话

脚本会自动检测并装 CPU 版 PyTorch（约 200MB，而 CUDA 版是 2.5GB）。
如果自动检测不准，可以强制指定：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Cpu
```

CPU 版唯一的影响是**音视频转写变慢**（1 小时音频约 15–25 分钟，CUDA 约 3–6 分钟）。
其他功能没有区别。而且有字幕的视频会直接抓字幕，根本不走转写。

---

## 先跑通再加装

依赖一次装全比较慢。想先确认能跑：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Minimal
```

最小安装可用：网页/公众号采集、浓缩、`list` / `show` / `cost` / `doctor`。

之后按需加装：

```powershell
.venv\Scripts\activate

pip install -e ".[search]"   # 语义检索与问答（ask / search / index）
pip install -e ".[av]"       # 音视频转写（B站等）
pip install -e ".[web]"      # 网页版（serve）
pip install -e ".[paper]"    # 论文模块
pip install -e ".[agent]"    # MCP / HTTP API
pip install -e ".[all]"      # 全部
```

---

## 网络慢的话换镜像

```powershell
.venv\Scripts\pip install -e ".[all]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

想永久生效：

```powershell
.venv\Scripts\pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 常见问题

**`distill` 命令找不到**
环境没激活。每次新开终端都要先 `.venv\Scripts\activate`，
或者直接用完整路径 `.venv\Scripts\distill.exe list`。

**中文显示成乱码**
旧版 cmd 的问题，换 Windows Terminal 或 PowerShell 7。
也可临时执行 `chcp 65001`。
（注意：这只影响终端显示，**存进库里的内容始终是 UTF-8**。）

**某个包装不上，提示需要 Microsoft Visual C++**
装 [Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，
或先用 `-Minimal` 跳过重依赖。

**`distill doctor` 说检索索引为 0**
带过来的库需要重建索引（向量数据没随包传输）：

```powershell
distill index --rebuild
```

首次会下载 embedding 模型（约 100MB）。

**想确认历史数据完整**

```powershell
distill list          # 应能看到之前消化的全部内容
distill show 1        # 浓缩结果应完整
```

数据库里只存相对路径，所以拷过来直接可用，不需要任何转换。

---

## 目录说明

```
insight-distiller\
├── src\distiller\      代码
├── data\               知识库（distiller.db 是本体）
├── config.yaml         你的配置（含 API Key，别外传）
├── config.example.yaml 配置模板与全部可选项说明
├── scripts\install.ps1 本安装脚本
└── README.md           完整功能文档
```

备份只需拷 `data\` 目录和 `config.yaml`。

"""打包成可传到 Windows 的 zip。

用法：
    python scripts/package.py                   # 只打代码（最小，约 200KB）
    python scripts/package.py --with-data       # 带知识库（db + 浓缩结果，不含音频）
    python scripts/package.py --with-data --with-audio   # 全带
    python scripts/package.py --with-models     # 连 embedding 模型缓存一起带

**为什么需要脚本而不是手动 zip**：有几样东西拷过去会造成难排查的故障，
而它们恰好都在项目目录里：

| 必须排除 | 后果 |
|----------|------|
| `.venv/` | 里面是 macOS 的二进制与写死的绝对路径，拷到 Windows 后 pip/python 全部失效 |
| `__pycache__/` | macOS 编译的 .pyc，Python 一般会忽略但会造成困惑 |
| `config.yaml` | **含 API Key**，不该进传输包 |
| `*.egg-info/` | 构建残留，内含本机绝对路径 |
| `data/audio/` | 音频缓存，动辄几 GB，而且转写结果已在库里，重下成本远低于传输 |

`data/distiller.db` 反而应该带上——库内只存相对路径（这是当初的设计决定），
拷过去直接能用，历史消化结果零转换。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 目录名黑名单：出现在任意层级都排除
EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".git", "build", "dist", "node_modules", ".idea", ".vscode",
    # 用工具跑出来的产物（他人内容 + 体积会长），说明见 cases/README.md。
    # 下面的 collect_code 用白名单本就不会收它，这里是双保险：
    # 防止以后有人把白名单改成 rglob 全量收集。
    "cases",
}

# 文件名/后缀黑名单
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".pyd", ".so", ".dylib", ".log"}
EXCLUDE_FILES = {
    "config.yaml",      # 含 API Key
    ".env",
    ".DS_Store",
}

# data/ 下按需包含的子目录
DATA_ALWAYS = ("distiller.db",)              # 知识库本体，必带
DATA_LIGHT = ("digests", "transcripts")      # 浓缩结果与转写文本，体积小价值高
DATA_HEAVY = ("audio", "cache", "papers")    # 音频与缓存，可重新生成


def _is_excluded(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix in EXCLUDE_SUFFIX:
        return True
    if path.name.endswith(".egg-info") or ".egg-info" in str(path):
        return True
    # 数据库的备份文件（迁移时自动生成的）不必带
    if ".backup-v" in path.name:
        return True
    return False


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def collect_code() -> list[Path]:
    """代码与文档。"""
    items: list[Path] = []
    for rel in ("src", "tests", "scripts"):
        base = ROOT / rel
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not _is_excluded(p.relative_to(ROOT)):
                items.append(p)
    for name in (
        "pyproject.toml", "README.md", "MANUAL.md", "TODO.md",
        "PLAN.md", "DECISIONS.md",
        "PORTABILITY.md", "AGENT_API.md", "KNOWLEDGE.md",
        "PAPERS.md", "DISCOVERY.md", "config.example.yaml",
        ".gitattributes", ".gitignore",
    ):
        p = ROOT / name
        if p.exists():
            items.append(p)
    return items


def collect_data(*, with_audio: bool) -> list[Path]:
    data = ROOT / "data"
    if not data.exists():
        return []

    items: list[Path] = []
    for name in DATA_ALWAYS:
        p = data / name
        if p.exists():
            items.append(p)
        # WAL 模式会有 -wal / -shm 旁文件。
        # 它们可能含未落盘的事务，一起带上更安全。
        for side in ("-wal", "-shm"):
            sp = data / f"{name}{side}"
            if sp.exists():
                items.append(sp)

    subdirs = list(DATA_LIGHT) + (list(DATA_HEAVY) if with_audio else [])
    for name in subdirs:
        base = data / name
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not _is_excluded(p.relative_to(ROOT)):
                items.append(p)
    return items


def find_model_cache() -> Path | None:
    """HuggingFace 模型缓存目录。

    embedding 模型约 100MB，Whisper large-v3 约 1.5GB。
    带上可省去目标机首次下载（国内网络下这一步经常是最大障碍）。
    """
    for env in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "SENTENCE_TRANSFORMERS_HOME"):
        v = os.environ.get(env)
        if v and Path(v).expanduser().exists():
            return Path(v).expanduser()
    default = Path.home() / ".cache" / "huggingface"
    return default if default.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description="打包项目以传输到 Windows")
    ap.add_argument("--out", default=None, help="输出 zip 路径")
    ap.add_argument("--with-data", action="store_true",
                    help="包含知识库（db + 浓缩结果 + 转写文本）")
    ap.add_argument("--with-audio", action="store_true",
                    help="连音频与缓存一起带（体积大，通常没必要）")
    ap.add_argument("--with-models", action="store_true",
                    help="包含 HuggingFace 模型缓存，省去目标机下载")
    ap.add_argument("--dry-run", action="store_true", help="只列清单不打包")
    args = ap.parse_args()

    if args.with_audio and not args.with_data:
        args.with_data = True

    files = collect_code()
    print(f"代码与文档：{len(files)} 个文件")

    if args.with_data:
        data_files = collect_data(with_audio=args.with_audio)
        size = sum(f.stat().st_size for f in data_files if f.exists())
        print(f"知识库数据：{len(data_files)} 个文件（{_human(size)}）")
        files += data_files
    else:
        print("知识库数据：跳过（用 --with-data 包含）")

    model_dir = None
    if args.with_models:
        model_dir = find_model_cache()
        if model_dir:
            msize = sum(
                f.stat().st_size for f in model_dir.rglob("*") if f.is_file()
            )
            print(f"模型缓存：{model_dir}（{_human(msize)}）")
        else:
            print("模型缓存：未找到，跳过")

    total = sum(f.stat().st_size for f in files if f.exists())
    print(f"\n合计（不含模型）：{_human(total)}")

    if args.dry_run:
        print("\n--dry-run，未生成文件。前 20 项：")
        for f in files[:20]:
            print(f"  {f.relative_to(ROOT)}")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = Path(args.out) if args.out else ROOT.parent / f"insight-distiller-{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    # ZIP_DEFLATED 而非 LZMA：Windows 资源管理器原生支持 deflate，
    # 双击即可解压，不需要装 7-zip
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            if not f.exists():
                continue
            zf.write(f, Path("insight-distiller") / f.relative_to(ROOT))

        if model_dir:
            for f in model_dir.rglob("*"):
                if f.is_file() and f.suffix not in EXCLUDE_SUFFIX:
                    zf.write(f, Path("hf-cache") / f.relative_to(model_dir))

        # 把安装说明写进包里，避免解压后不知道下一步做什么
        readme = ROOT / "scripts" / "WINDOWS_SETUP.md"
        if readme.exists():
            zf.write(readme, "开始安装.md")

    print(f"\n已生成 {out}")
    print(f"实际体积 {_human(out.stat().st_size)}")
    print("\n传到 Windows 后：解压 → 读「开始安装.md」→ 双击 scripts\\install.ps1")
    if model_dir:
        print("（包内 hf-cache 需按说明放到 %USERPROFILE%\\.cache\\huggingface）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

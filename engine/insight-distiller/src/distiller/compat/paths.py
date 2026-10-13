"""路径与文件名处理。

跨平台要点：
1. 全程使用 pathlib，禁止字符串拼接路径。
2. 文件名必须过 safe_filename()——Windows 禁止 \\ / : * ? " < > | 且有保留设备名。
3. 数据库内只存**相对路径**，整个项目文件夹拷到 Windows 后无需任何转换即可使用。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Windows 保留设备名（不区分大小写，且带任意扩展名也不行）
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 文件名主干最大长度。Windows 完整路径上限 260 字符，
# 留足空间给目录前缀与扩展名，避免深层目录下超限。
_MAX_STEM = 80


def safe_filename(name: str, *, fallback: str = "untitled") -> str:
    """把任意标题转换为三平台都合法的文件名主干。

    处理：非法字符、Windows 保留名、首尾空格与点、超长截断。
    截断时追加内容哈希，避免不同标题被截成同一个文件名而互相覆盖。
    """
    # NFC 归一：macOS 的 HFS+/APFS 倾向 NFD，Windows 用 NFC，
    # 不归一会导致同名中文文件在两个平台被视为不同文件。
    name = unicodedata.normalize("NFC", name or "")
    cleaned = _ILLEGAL_CHARS.sub("_", name)
    # Windows 不允许文件名以空格或点结尾
    cleaned = cleaned.strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)

    if not cleaned:
        cleaned = fallback

    if cleaned.split(".")[0].upper() in _WIN_RESERVED:
        cleaned = f"_{cleaned}"

    if len(cleaned) > _MAX_STEM:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[: _MAX_STEM - 9].rstrip()}-{digest}"

    return cleaned


@dataclass(frozen=True)
class DataPaths:
    """运行时数据目录集合。

    root 默认是项目内的 ./data，好处是整个项目文件夹可直接拷贝迁移。
    也可通过配置切换到系统标准目录（platformdirs）。
    """

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "distiller.db"

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def digests(self) -> Path:
        return self.root / "digests"

    @property
    def papers(self) -> Path:
        return self.root / "papers"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def vector(self) -> Path:
        return self.root / "lancedb"

    def ensure(self) -> None:
        for p in (
            self.root, self.audio, self.transcripts,
            self.digests, self.papers, self.cache,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """把绝对路径转为相对 root 的 POSIX 风格字符串，用于入库存储。

        统一用正斜杠，使数据库内容与平台无关。
        """
        try:
            rel = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return path.as_posix()
        return rel.as_posix()

    def absolute(self, rel: str) -> Path:
        """把库中存的相对路径还原为当前平台的绝对路径。"""
        return self.root / Path(rel)


def get_data_paths(root: str | Path | None = None, *, use_system_dir: bool = False) -> DataPaths:
    """解析数据目录。

    优先级：显式传入 > 系统标准目录（若开启）> 项目内 ./data
    """
    if root is not None:
        return DataPaths(Path(root).expanduser())

    if use_system_dir:
        from platformdirs import user_data_dir

        return DataPaths(Path(user_data_dir("insight-distiller", appauthor=False)))

    # 项目根目录 = 本文件上溯四级（compat -> distiller -> src -> 项目根）
    project_root = Path(__file__).resolve().parents[3]
    return DataPaths(project_root / "data")

"""Embedding 抽象。

设计：模型可配置。默认 `bge-small-zh-v1.5`（约 100MB，加载快）；
追求中文效果可换 `bge-m3`（约 2.2GB，长文本与多语言更强）。
论文模块（M3）将另用 SPECTER2，因此这里做成按用途取模型。

首次使用会下载模型，因此加载做懒初始化 + 明确进度提示。
"""

from __future__ import annotations

import contextlib
import os
from typing import Callable, Iterator

ProgressFn = Callable[[str], None]


@contextlib.contextmanager
def _quiet_loading() -> Iterator[None]:
    """临时静音模型加载过程的进度条与告警。

    这些输出来自 huggingface/transformers 内部，会污染 CLI 表格与管道输出，
    在 Windows 旧终端下还可能产生乱码。
    """
    saved = {
        k: os.environ.get(k)
        for k in ("HF_HUB_DISABLE_PROGRESS_BARS", "TRANSFORMERS_VERBOSITY", "TQDM_DISABLE")
    }
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["TRANSFORMERS_VERBOSITY"] = "error"
    os.environ["TQDM_DISABLE"] = "1"
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """句向量编码器。单例式懒加载，避免重复加载模型。"""

    def __init__(self, model_name: str, *, progress: ProgressFn | None = None) -> None:
        self.model_name = model_name
        self.progress = progress or (lambda _m: None)
        self._model = None
        self._dim: int | None = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                '未安装 sentence-transformers。请执行：pip install -e ".[search]"'
            ) from exc

        self.progress(f"加载向量模型 {self.model_name}（首次会下载）")
        try:
            # 抑制模型加载时的进度条与告警：它们会污染 CLI 输出，
            # 且在管道重定向/Windows 旧终端下可能产生乱码。
            with _quiet_loading():
                self._model = SentenceTransformer(self.model_name)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"向量模型加载失败: {exc}") from exc
        return self._model

    @property
    def dim(self) -> int:
        model = self._load()
        if self._dim is None:
            # sentence-transformers 6.x 改名，兼容两种 API
            getter = getattr(model, "get_embedding_dimension", None) or getattr(
                model, "get_sentence_embedding_dimension"
            )
            self._dim = int(getter())
        return self._dim

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        """编码文本。归一化后可直接用点积当余弦相似度。"""
        if not texts:
            return []
        model = self._load()

        # bge 系列检索任务要求 query 加指令前缀，否则召回质量明显下降
        if is_query and "bge" in self.model_name.lower():
            if "zh" in self.model_name.lower():
                texts = [f"为这个句子生成表示以用于检索相关文章：{t}" for t in texts]

        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vectors]

    def encode_one(self, text: str, *, is_query: bool = False) -> list[float]:
        return self.encode([text], is_query=is_query)[0]

"""OCR 抽象层。

**跨平台关键决定**：默认用 RapidOCR 而非 macOS 原生 Vision。
RapidOCR 基于 ONNXRuntime，三平台通用，能保证 macOS 与 Windows 上
OCR 结果一致——否则迁移后会出现"摘要质量变了"这种极难排查的问题。
ocrmac 仅作为 macOS 上的可选加速项。

公众号大量文章正文是长图，没有 OCR 就只能抽到空文章，所以这是 P0 能力。
"""

from __future__ import annotations

from typing import Protocol

from ..compat.caps import detect_caps


class OCREngine(Protocol):
    def read(self, image_bytes: bytes) -> str: ...


class RapidOCREngine:
    """三平台通用实现（默认）。

    需同时兼容两个包，因为 RapidOCR 在 2.x 时改了包名：

    | 包 | Python 支持 | 返回格式 |
    |----|-------------|----------|
    | `rapidocr-onnxruntime` 1.x | **仅 <3.13** | `(list[[box, text, score]], elapse)` 元组 |
    | `rapidocr` 3.x | 3.13/3.14 可用 | `RapidOCROutput` 对象，文本在 `.txts` |

    不兼容的后果很实际：python.org 现在默认下载 3.13+，
    只依赖旧包会让新机器 `pip install` 直接失败。
    """

    name = "rapidocr"

    def __init__(self) -> None:
        self._new_api = False
        try:
            # 优先新包：它支持更新的 Python
            from rapidocr import RapidOCR

            self._new_api = True
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()

    def read(self, image_bytes: bytes) -> str:
        import io

        if self._new_api:
            # 新版直接吃 bytes，返回对象
            result = self._ocr(image_bytes)
            txts = getattr(result, "txts", None)
            return "\n".join(txts) if txts else ""

        result, _ = self._ocr(io.BytesIO(image_bytes))
        if not result:
            return ""
        # result 每项为 [box, text, score]
        return "\n".join(item[1] for item in result if len(item) > 1)


class OCRMacEngine:
    """macOS 原生 Vision，仅作可选加速。"""

    name = "ocrmac"

    def __init__(self) -> None:
        # 平台可用性判断统一交给 compat 层，业务代码不做平台分支
        if "ocrmac" not in detect_caps().ocr_engines:
            raise RuntimeError("当前平台不支持 ocrmac（仅 macOS 且需安装 ocrmac 包）")
        from ocrmac import ocrmac  # noqa: F401

        self._ocrmac = ocrmac

    def read(self, image_bytes: bytes) -> str:
        import tempfile
        from pathlib import Path

        # ocrmac 只接受文件路径，需落临时文件
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(image_bytes)
            tmp = Path(f.name)
        try:
            annotations = self._ocrmac.OCR(
                str(tmp), language_preference=["zh-Hans", "en-US"]
            ).recognize()
            return "\n".join(a[0] for a in annotations)
        finally:
            tmp.unlink(missing_ok=True)


def get_ocr_engine(preference: str = "auto") -> OCREngine | None:
    """按配置与平台能力选择 OCR 引擎。

    preference: auto | rapidocr | ocrmac | off
    auto 优先 RapidOCR，保证跨平台一致性。
    """
    if preference == "off":
        return None

    order = {
        "auto": ["rapidocr", "ocrmac"],
        "rapidocr": ["rapidocr"],
        "ocrmac": ["ocrmac", "rapidocr"],
    }.get(preference, ["rapidocr"])

    for name in order:
        try:
            if name == "rapidocr":
                return RapidOCREngine()
            if name == "ocrmac":
                return OCRMacEngine()
        except Exception:  # noqa: BLE001 - 引擎不可用则尝试下一个
            continue
    return None

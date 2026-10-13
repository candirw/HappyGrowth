"""音频下载与转码。

用 yt-dlp 只下音轨（不下视频，省带宽与时间），再转成 16k 单声道 wav
——Whisper 的输入要求。

**为什么自己转码而不交给 yt-dlp 的后处理器**：

yt-dlp 在 `ffmpeg_location` 目录下按标准名查找 `ffmpeg` 与 `ffprobe`。
而 imageio-ffmpeg 自带的二进制叫 `ffmpeg-macos-aarch64-v7.1` 这类
带平台后缀的名字，**且完全不提供 ffprobe**。
结果是：我们能找到并调用 ffmpeg，但 yt-dlp 找不到，报
"ffprobe and ffmpeg not found" 然后整条转写链路失败。

因此改为：yt-dlp 只负责下载原始音频流，转码由我们用已解析的确切路径执行。
这样既不依赖二进制文件名，也不需要 ffprobe。

跨平台：ffmpeg 路径由 compat.binaries 解析（配置 > PATH > imageio-ffmpeg 自带），
Windows 无需任何系统级安装。
"""

from __future__ import annotations

from pathlib import Path

from ..compat.binaries import find_ffmpeg, run
from ..compat.paths import safe_filename
from ..config import IngestConfig


class AudioError(RuntimeError):
    pass


# yt-dlp 可能产出的音频容器
_AUDIO_SUFFIXES = (".m4a", ".webm", ".opus", ".mp3", ".aac", ".ogg", ".mp4", ".flac")


def download_audio(
    url: str,
    out_dir: Path,
    *,
    config: IngestConfig,
    basename: str | None = None,
) -> Path:
    """下载音轨并转成 16kHz 单声道 wav。若已存在则直接复用（缓存）。"""
    try:
        import yt_dlp
    except ImportError as exc:
        raise AudioError(
            '未安装 yt-dlp。请执行：pip install -e ".[av]"'
        ) from exc

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise AudioError(
            '未找到 ffmpeg。请执行：pip install -e ".[av]"（含自带二进制），'
            "或自行安装并在 config.yaml 的 binaries.ffmpeg 指定路径。"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(basename or url.rsplit("/", 1)[-1] or "audio")
    target = out_dir / f"{stem}.wav"
    if target.exists() and target.stat().st_size > 1024:
        return target

    # 只下载，不做后处理——后处理需要 yt-dlp 自己找到 ffmpeg/ffprobe，
    # 而自带二进制的文件名不符合它的查找规则。
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / f"{stem}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if config.proxy:
        opts["proxy"] = config.proxy
    if config.cookies_from_browser:
        opts["cookiesfrombrowser"] = (config.cookies_from_browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 - yt-dlp 抛多种异常类型
        raise _download_error(exc, config) from exc

    raw = _find_downloaded(out_dir, stem)
    if raw is None:
        raise AudioError("音频下载完成但未找到输出文件")

    if raw.suffix.lower() == ".wav":
        return raw

    try:
        _to_whisper_wav(ffmpeg, raw, target)
    except RuntimeError:
        # 转码失败时保留原始文件：faster-whisper 内部用 PyAV 解码，
        # 多数容器能直接读，不该因为转码失败就放弃整条链路。
        return raw
    raw.unlink(missing_ok=True)
    return target


def _find_downloaded(out_dir: Path, stem: str) -> Path | None:
    """找到 yt-dlp 实际产出的文件。扩展名取决于平台提供的音频流格式。"""
    exact = [
        p
        for p in out_dir.glob(f"{stem}.*")
        if p.suffix.lower() in (".wav",) + _AUDIO_SUFFIXES
        and p.stat().st_size > 1024
    ]
    if not exact:
        return None
    # wav 优先（无需转码），其余取最大的那个
    for p in exact:
        if p.suffix.lower() == ".wav":
            return p
    return max(exact, key=lambda p: p.stat().st_size)


def _to_whisper_wav(ffmpeg: Path, src: Path, dst: Path) -> None:
    """转成 Whisper 需要的 16kHz 单声道 PCM。

    用已解析的确切路径调用，不受二进制文件名约束，也不需要 ffprobe。
    """
    run(
        [
            ffmpeg,
            "-y",
            "-loglevel", "error",
            "-i", src,
            "-vn",
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            dst,
        ],
        timeout=1800,
    )
    if not dst.exists() or dst.stat().st_size <= 1024:
        raise RuntimeError("ffmpeg 输出为空")


def _download_error(exc: Exception, config: IngestConfig) -> AudioError:
    text = str(exc)
    # 412/403 是风控而非网络故障，原始报错对用户毫无指引。
    # B 站对未登录请求限流很激进，配 cookie 基本就能解决。
    if "412" in text or "403" in text or "Precondition Failed" in text:
        hint = (
            "平台反爬拦截（HTTP 412/403）。通常配置浏览器 cookie 即可解决：\n"
            "    在 config.yaml 设置 ingest.cookies_from_browser: chrome"
            "（或 edge / firefox / safari）\n"
            "    若仍失败，可能是短时间请求过多，等几分钟再试。"
        )
        if not config.cookies_from_browser:
            return AudioError(f"音频下载失败：{hint}")
        return AudioError(
            f"音频下载失败：{hint}\n"
            f"    当前已配置 cookies_from_browser="
            f"{config.cookies_from_browser}，请确认该浏览器已登录对应站点。"
        )
    return AudioError(f"音频下载失败: {exc}")

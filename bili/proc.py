"""底层执行：子进程、工具探测、时间格式化。

这里只管"把命令跑起来、把结果拿回来"，不认识 B 站，也不认识画面和文字。
"""

import asyncio
import os
import shutil
import subprocess


NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def locate_ffmpeg(configured: str) -> str:
    configured = (configured or "").strip().strip('"')
    if configured:
        if os.path.isfile(configured):
            return configured
        found = shutil.which(configured)
        if found:
            return found
    for name in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(name)
        if found:
            return found
    try:  # 随依赖装的 ffmpeg（imageio-ffmpeg），使用者不用自己配 PATH
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.isfile(bundled):
            return bundled
    except Exception:  # noqa: BLE001
        pass
    for guess in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if os.path.isfile(guess):
            return guess
    return ""


async def run_cmd(args: list[str], timeout: float = 120) -> tuple[int, str]:
    def go():
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
            return p.returncode, (p.stdout or b"") + (p.stderr or b"")
        except subprocess.TimeoutExpired:
            return -1, b"timeout"
        except Exception as e:  # noqa: BLE001
            return -2, str(e).encode("utf-8", "ignore")

    code, raw = await asyncio.to_thread(go)
    return code, raw.decode("utf-8", "ignore")


async def run_cmd_bytes(args: list[str], timeout: float = 240) -> tuple[int, bytes]:
    """跑命令拿原始字节（解码低清画面用，不能走文本通道）。"""

    def go():
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
            return p.returncode, p.stdout
        except Exception:  # noqa: BLE001
            return -1, b""

    return await asyncio.to_thread(go)


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"

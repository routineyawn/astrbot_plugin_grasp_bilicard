"""下载：yt-dlp 调参、失败降级、错误分类、媒体信息。
"""

import asyncio
import json
import os
import re
import time
from astrbot.api import logger

from .http import cookie_header
from .proc import run_cmd
from .security import SHORT_HOSTS, bili_cookie, url_host


# 视频 CDN 主机名，只在日志里用来定位"哪台节点连不上"，不参与业务判断
CDN_RE = re.compile(r"(?:[a-z0-9-]+\.)+(?:bilivideo\.com|akamaized\.net)", re.I)


def cdn_hosts(msg: str) -> list[str]:
    """从 yt-dlp 的报错里挑出节点主机名，去重后按字母序返回。"""
    return sorted({m.lower() for m in CDN_RE.findall(msg or "")})


def classify_error(msg: str) -> str:
    """把下载异常归成用户能看懂的一类。"""
    m = (msg or "").lower()
    if any(k in m for k in ("404", "not found", "不存在", "已被删除", "啥都木有")):
        return "not_found"
    if any(k in m for k in ("vip", "会员", "premium", "need login", "login required")):
        return "vip"
    if any(
        k in m
        for k in (
            "timed out",
            "timeout",
            "connection",
            "ssl",
            "network",
            "urlopen",
            "ffmpeg exited",
        )
    ):
        return "network"
    if any(k in m for k in ("max-filesize", "larger than", "超过")):
        return "too_big"
    return "other"
class _Lines:
    """把 yt-dlp 的 verbose 输出收进内存，失败时只打最后几行给日志。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg) -> None:
        if str(msg).strip():
            self.lines.append(str(msg))

    def info(self, msg) -> None:
        self.debug(msg)

    def warning(self, msg) -> None:
        self.debug(f"WARNING: {msg}")

    def error(self, msg) -> None:
        self.debug(f"ERROR: {msg}")


# 这些错误多半是一时的（片段坏了、节点抖了一下），清掉半截文件再来一次往往就成
RETRY_RE = re.compile(
    r"ffmpeg exited|timed out|timeout|connection|reset by peer|broken pipe|4294967",
    re.I,
)


def retryable(msg: str) -> bool:
    """这个错误值不值得重试一次。"""
    return bool(RETRY_RE.search(msg or ""))


# 同一台节点、同一条流合并失败，再试一次也是同一个错——直接换备用地址
MERGE_FAIL_RE = re.compile(r"ffmpeg exited", re.I)


def merge_failed(msg: str) -> bool:
    """是不是「流拿到了但合并不了」这类重试无用的错。"""
    return bool(MERGE_FAIL_RE.search(msg or ""))


async def media_info(ffmpeg: str, path: str) -> tuple[float, bool]:
    """返回 (时长秒, 有没有音轨)。"""
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe") if os.path.dirname(ffmpeg) else ""
    if os.path.isfile(probe):
        code, out = await run_cmd(
            [probe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", path],
            30,
        )
        if code == 0 and "{" in out:
            try:
                obj = json.loads(out[out.find("{"):])
                dur = float(obj.get("format", {}).get("duration") or 0)
                audio = any(s.get("codec_type") == "audio" for s in obj.get("streams", []))
                return dur, audio
            except Exception:  # noqa: BLE001
                pass
    code, out = await run_cmd([ffmpeg, "-hide_banner", "-i", path], 30)
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, "Audio:" in out


def _has_video(out_dir: str) -> bool:
    """目录里有没有下完的视频（半截文件不算）。"""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return False
    return any(
        name.startswith("video.") and not name.endswith((".part", ".ytdl", ".temp"))
        for name in names
    )


async def download(
    url: str,
    out_dir: str,
    ffmpeg: str,
    max_mb: int,
    cookie: str = "",
    max_height: int = 480,
    seconds: int = 0,
) -> tuple[str, str, float]:
    """下一条视频，返回 (文件路径, 失败原因, 视频真实时长)。

    只下需要的片段（``seconds`` 秒以内）：画面只看前几分钟，把整条下载下来
    是白花流量和时间——实测一条 10 分钟的视频，前 5 分钟只占一半。
    真实时长要单独返回，因为截断后 ffprobe 量出来的是片段长度，字幕整片覆盖，
    拿片段长度去校验字幕会误判成"覆盖时长对不上"。
    """
    t1 = time.perf_counter()
    try:
        import yt_dlp
    except ImportError:
        logger.warning("[bili_card] 没装 yt-dlp，跳过下载")
        return "", "no_ytdlp", 0.0
    os.makedirs(out_dir, exist_ok=True)
    template = os.path.join(out_dir, "video.%(ext)s")

    # yt-dlp 不认短链（b23.tv / bili2233.cn）：它会当成普通网页，
    # 最后让 ffmpeg 去下 HTML 而失败（日志里就是 ffmpeg exited with code 4294967158）。
    # 先自己解析成 BV 链接。
    target = url
    if url_host(url) in SHORT_HOSTS:
        from .http import _bvid_of, page_of

        bv = await _bvid_of(url, {"Referer": "https://www.bilibili.com/"})
        if bv:
            # 分 P 要一起带过去：番剧/课程/合集都是一个 BV 挂多话，
            # 不带 p 的话 yt-dlp 下的是第 1 话
            page = page_of(url)
            target = f"https://www.bilibili.com/video/{bv}?p={page}"
            if page > 1:
                logger.info(f"[bili_card] 短链解析成 {bv} 的 P{page}，再交给 yt-dlp")
            else:
                logger.info(f"[bili_card] 短链解析成 {bv}，再交给 yt-dlp")
        else:
            # 解析不出来（短链过期/被限流）时，把 b23 网址交给 yt-dlp 只会让它
            # 当成网页、再让 ffmpeg 下 HTML，白等二十多秒后失败。直接快失败。
            logger.info("[bili_card] 短链解析不出 BV 号（可能过期或被限流），不再硬试下载")
            return "", "not_found", 0.0
    if "bilibili.com/video/" in target:
        from .http import page_of

        logger.info(f"[bili_card] 这次处理的是 P{page_of(url)}")

    def go(verbose: bool = False, sink: "_Lines | None" = None, no_audio: bool = False):
        # 宫格每格只有 350×194 左右，480P 足够；逐帧模式才要 720P
        # portrait videos report height as the long edge (480P is 480x852),
        # so also match on width and keep a plain best-video fallback
        if no_audio:
            # 声音那一轨在某些 CDN 节点上老是失败，合并不了整条就白下；
            # 只要画面的话退一步：拿不到声音，至少宫格能看
            fmt = (
                f"bv*[ext=mp4][height<={max_height}]"
                f"/bv*[ext=mp4][width<={max_height}]"
                f"/bv*[ext=mp4]/bv*"
            )
        else:
            fmt = (
                f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]"
                f"/bv*[ext=mp4][width<={max_height}]+ba[ext=m4a]"
                f"/bv*[ext=mp4]+ba[ext=m4a]"
                f"/bv*+ba/b"
            )
        opts = {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": template,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            # 节点连不上时，等 20 秒 ×3 次等于白等 64 秒；短超时快点失败，尽快换故事板
            "socket_timeout": 5,
            "retries": 1,
            "max_filesize": max(10, int(max_mb)) * 1024 * 1024,
            "ffmpeg_location": ffmpeg,
        }
        if verbose:
            # 只在失败后再跑一遍：quiet 会把"哪个流、哪一步、ffmpeg 命令行"全吞掉
            opts["quiet"] = False
            opts["no_warnings"] = False
            opts["verbose"] = True
            opts["logger"] = sink if sink is not None else _Lines()
        if seconds > 0:
            opts["download_ranges"] = yt_dlp.utils.download_range_func(
                None, [(0, int(seconds))]
            )
            # 不重编码：切在关键帧上，够看画面
            opts["force_keyframes_at_cuts"] = False
        ck = cookie_header(bili_cookie(url, cookie))
        if ck:
            opts["http_headers"] = {
                "Cookie": ck,
                "Referer": "https://www.bilibili.com/",
            }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True) or {}
        box_dur.append(float(info.get("duration") or 0))

    def clear_partials() -> None:
        """重试前删掉半截文件：接着下同一个坏片段，多半还是同一个错。"""
        for name in os.listdir(out_dir):
            if name.startswith("video.") and name.endswith((".part", ".ytdl", ".temp")):
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass

    box: list[str] = []
    box_dur: list[float] = []
    for attempt in (1, 2):
        try:
            await asyncio.to_thread(go)
        except Exception as e:  # noqa: BLE001
            box.append(str(e))
            logger.info(f"[bili_card] 下载失败（第 {attempt} 次）：{e}")
            for h in cdn_hosts(str(e)):
                logger.info(f"[bili_card] 失败节点：{h}")
            if attempt == 1 and merge_failed(str(e)):
                logger.info("[bili_card] 合并失败重试也没用，直接换备用地址")
                break
            if attempt == 1 and retryable(str(e)):
                clear_partials()
                logger.info("[bili_card] 这类错误可能是一时的，清掉半截文件再试一次")
                await asyncio.sleep(1.0)
                continue
            break
    # 画面 + 声音合并不了时，声音那轨通常是元凶；退回只下画面再试一次。
    # 但如果失败原因是 ffmpeg 合并本身出不来（多半是那条音频节点坏了），
    # 换个 format 还是同一台坏节点，不如直接去踩备用地址。
    hopeless = bool(box) and merge_failed(box[0])
    if box and not hopeless and not _has_video(out_dir):
        try:
            await asyncio.to_thread(go, False, None, True)
            logger.info("[bili_card] 退回只下画面（没有声音），已经有画面就不再试合并")
        except Exception as e:  # noqa: BLE001
            box.append(str(e))
            logger.info(f"[bili_card] 只下画面也失败：{e}")

    # yt-dlp 只认第一个节点，坏了就整条废；B 站其实给了 2–3 个备用地址
    if box and not _has_video(out_dir):
        from .play import fetch as fetch_dash

        path, real_dur = await fetch_dash(
            target,
            out_dir,
            ffmpeg,
            cookie_header(bili_cookie(url, cookie)),
            max_height,
            seconds,
        )
        if path:
            logger.info(
                f"[bili_card] 改用备用地址成功：{os.path.getsize(path) // 1024} KB，"
                f"{time.perf_counter() - t1:.1f} 秒"
            )
            return path, "", real_dur
        logger.info("[bili_card] 备用地址也没成，退到故事板")

    if box and (retryable(box[-1]) or classify_error(box[-1]) == "other"):
        sink = _Lines()
        try:
            await asyncio.wait_for(asyncio.to_thread(go, True, sink), 45)
        except Exception:  # noqa: BLE001
            pass
        tail = [ln for ln in sink.lines if ln.strip()][-20:]
        if tail:
            logger.info(
                f"[bili_card] yt-dlp 详细输出（最后 {len(tail)} 行）：\n"
                + "\n".join(tail)
            )
    exts = (".mp4", ".webm", ".mkv", ".flv", ".m4v", ".mov", ".ts")
    for name in sorted(os.listdir(out_dir)):
        if not name.startswith("video"):
            continue
        if name.endswith((".part", ".ytdl", ".temp")):
            continue
        p = os.path.join(out_dir, name)
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                head = f.read(16)
        except OSError:
            continue
        # 短链失效时 yt-dlp 会把错误 JSON 当文件存下来，靠大小和文件头挡掉
        if size < 50 * 1024 or len(head) < 8:
            continue
        looks_video = (
            name.lower().endswith(exts)
            and (
                head[4:8] == b"ftyp"
                or head[:4] == b"\x1aE\xdf\xa3"  # webm
                or head[:3] == b"FLV"
                or head[:4] == b"RIFF"  # avi
            )
        )
        if looks_video:
            logger.info(
                f"[bili_card] 下载完成：{size // 1024} KB，{time.perf_counter() - t1:.1f} 秒"
            )
            return p, "", (box_dur[0] if box_dur else 0.0)
    return "", (classify_error(box[0]) if box else "other"), 0.0

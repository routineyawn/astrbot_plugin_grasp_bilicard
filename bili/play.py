"""B 站官方播放地址（playurl）：给下载留一条备用路。

yt-dlp 挑地址的方式是"取第一个"，而 B 站 playurl 对每一路流都给
2–3 个不同节点（``backupUrl``）。第一个节点连不上时，yt-dlp 就整条失败，
我们却没有接口去换——所以这里自己把地址取回来，按顺序挨个试。

只在 yt-dlp 彻底失败后才走这条：正常情况仍然交给 yt-dlp，省事。
"""

import asyncio
import os
import re
import time

from astrbot.api import logger

from .http import BILI_UA, _bili_json, _bvid_of, page_cid, page_of
from .proc import run_cmd


# PCDN 是"借别人家宽带"的节点，时好时坏，能绕开就绕开
PCDN_HOSTS = (
    "mcdn.bilivideo.com",
    "mcdn.bilivideo.cn",
    "szbdyd.com",
    "mountaintoys.cn",
    "nexusedgeio.com",
    "ahdohpiechei.com",
)

HEADERS = {"Referer": "https://www.bilibili.com/", "User-Agent": BILI_UA}


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def is_pcdn(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in PCDN_HOSTS)


def url_order(node: dict) -> list[str]:
    """一路流的可用地址：主地址 + backupUrl，去重，PCDN 排到最后。"""
    seen: dict[str, None] = {}
    for url in [node.get("baseUrl") or ""] + list(node.get("backupUrl") or []):
        if url and url not in seen:
            seen[url] = None
    return sorted(seen, key=is_pcdn)


def _score(node: dict, max_height: int) -> tuple:
    w = int(node.get("width") or 0)
    h = int(node.get("height") or 0)
    short = min(w, h) if w and h else 0
    fits = bool(short) and short <= max_height
    codec = str(node.get("codecs") or "")
    # 够用的排前；同样够用的编码 avc 优先（解码最稳）；再要最清晰的
    return (0 if fits else 1, 0 if codec.startswith("avc") else 1,
            -short if fits else short)


def order_videos(videos: list[dict], max_height: int) -> list[dict]:
    """按"够用且清晰"排好的画面流。

    换清晰度也等于换一批节点——同一个清晰度的备用地址常常指向同一家坏节点，
    所以备用路要连着几档一起试。
    """
    return sorted(videos, key=lambda v: _score(v, max_height))


def pick_video(videos: list[dict], max_height: int) -> dict:
    """挑一路够用的画面：短边不超过上限里最清晰的，优先 avc。

    竖屏视频的 height 是长边（480x852），所以按短边判断。
    """
    if not videos:
        return {}
    return order_videos(videos, max_height)[0]


def pick_audio(audios: list[dict]) -> dict:
    """声音只要够转写，挑码率最低的那一路，省流量。"""
    if not audios:
        return {}
    return sorted(audios, key=lambda a: int(a.get("bandwidth") or 0))[0]


async def _save(client, url: str, path: str) -> bool:
    try:
        size = 0
        with open(path, "wb") as f:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                async for chunk in resp.aiter_bytes(256 * 1024):
                    f.write(chunk)
                    size += len(chunk)
        return size > 50 * 1024
    except Exception:  # noqa: BLE001
        return False


async def _probe(client, url: str, timeout: float = 3.0) -> bool:
    """只取头 2KB 看节点活不活：比整个下完再失败便宜得多。"""
    try:
        async with client.stream(
            "GET", url, headers={"Range": "bytes=0-2047"}, timeout=timeout
        ) as resp:
            if resp.status_code not in (200, 206):
                return False
            async for _ in resp.aiter_bytes(1024):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def _merge(ffmpeg: str, video: str, audio: str, out: str, seconds: int) -> bool:
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    if audio:
        args += ["-i", audio]
    args += ["-c", "copy"]
    if seconds > 0:
        args += ["-t", str(int(seconds))]
    args += [out]
    code, text = await run_cmd(args, 180)
    if code != 0:
        logger.info(f"[bili_card] 备用地址合并失败：{text[-200:]}")
        return False
    return os.path.isfile(out) and os.path.getsize(out) > 50 * 1024


async def _save_audio(client, audios: list[dict], path: str) -> bool:
    """音频按码率从低到高试：够转写就行，先试最小的那路。"""
    tried = 0
    for node in sorted(audios, key=lambda a: int(a.get("bandwidth") or 0)):
        for url in url_order(node)[:2]:
            if tried >= 6:
                return False
            tried += 1
            if await _save(client, url, path):
                return True
    return False


async def _audio_from_durl(client, bvid: str, cid: int, ffmpeg: str, path: str,
                           seconds: int, headers: dict) -> bool:
    """DASH 音频节点整批不可达时，用 html5 接口的合成流只把声音抠出来。

    durl 给的是音视频合一的整段文件，让 ffmpeg 直接读地址、只留前 seconds 秒的音轨，
    不把整条视频落盘。
    """
    data = await _bili_json(
        client,
        "https://api.bilibili.com/x/player/playurl",
        {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 1, "fnver": 0,
         "platform": "html5", "high_quality": 1},
        headers,
    )
    nodes = (data.get("data") or {}).get("durl") or []
    urls: list[str] = []
    for node in nodes[:2]:
        for url in [node.get("url")] + list(node.get("backup_url") or [])[:2]:
            if url:
                urls.append(url)
    if not urls:
        logger.info("[bili_card] html5 接口也没给音频地址")
        return False
    for url in urls[:3]:
        args = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-headers", "Referer: https://www.bilibili.com/\r\n",
            "-user_agent", BILI_UA,
            "-i", url,
        ]
        if seconds > 0:
            args += ["-t", str(int(seconds))]
        # ffmpeg 靠扩展名认封装，.m4s 它不认，先写成 .m4a 再挪回去
        tmp = path[:-4] + ".m4a" if path.endswith(".m4s") else path + ".m4a"
        args += ["-vn", "-c:a", "aac", "-b:a", "64k", "-f", "ipod", tmp]
        code, text = await run_cmd(args, 120)
        if code == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 20 * 1024:
            os.replace(tmp, path)
            logger.info(f"[bili_card] 声音从 html5 接口拿到了：{_host(url)}")
            return True
        logger.info(f"[bili_card] html5 取声音失败：{text[-120:]}")
    return False


async def fetch(
    url: str,
    out_dir: str,
    ffmpeg: str,
    cookie: str = "",
    max_height: int = 480,
    seconds: int = 300,
) -> tuple[str, float]:
    """用 playurl 的地址自己下一份，返回 (文件路径, 视频真实时长)；失败 ("", 0.0)。"""
    try:
        import httpx
    except ImportError:
        return "", 0.0
    if not ffmpeg:
        return "", 0.0
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    os.makedirs(out_dir, exist_ok=True)
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True
    ) as client:
        bvid = await _bvid_of(url, headers)
        if not bvid:
            logger.info("[bili_card] 备用地址：认不出 BV 号")
            return "", 0.0
        view = await _bili_json(
            client,
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": bvid},
            headers,
        )
        info = view.get("data") or {}
        cid, duration = page_cid(info, page_of(url))
        if not cid:
            logger.info(f"[bili_card] 备用地址：取不到 cid（code={view.get('code')}）")
            return "", 0.0
        play = await _bili_json(
            client,
            "https://api.bilibili.com/x/player/playurl",
            {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 16, "fnver": 0, "fourk": 0},
            headers,
        )
        dash = (play.get("data") or {}).get("dash") or {}
        video = pick_video(dash.get("video") or [], max_height)
        audios = dash.get("audio") or []
        if not video:
            logger.info(
                f"[bili_card] 备用地址：没有可用的画面流（code={play.get('code')}）"
            )
            return "", 0.0
        v_file = os.path.join(out_dir, "_dash_v.m4s")
        a_file = os.path.join(out_dir, "_dash_a.m4s")
        out = os.path.join(out_dir, "video.mp4")
        # 同清晰度的备用地址常指向同一家坏节点，所以连着几档一起试；
        # 但整条兜底不能久留——超时就交给故事板，别让用户等
        deadline = time.monotonic() + 40
        cands: list[str] = []
        for node in order_videos(dash.get("video") or [], max_height)[:3]:
            cands.extend(url_order(node)[:2])
        if not cands:
            logger.info("[bili_card] 备用地址：没有可用的画面流")
            return "", 0.0
        # 先并发探活：连着两个死节点各等一次超时太贵，探一遍只要 3 秒
        alive = await asyncio.gather(*(_probe(client, u) for u in cands))
        order = [u for u, ok in zip(cands, alive) if ok]
        order += [u for u, ok in zip(cands, alive) if not ok]
        tried = 0
        for v_url in order:
            if tried >= 4 or time.monotonic() > deadline:
                break
            tried += 1
            if not await _save(client, v_url, v_file):
                logger.info(f"[bili_card] 备用地址连不上：{_host(v_url)}")
                continue
            audio_ok = await _save_audio(client, audios, a_file)
            if not audio_ok:
                # 音频的几个节点常常出自同一家，一家挂了就整批挂，换个接口再捞
                audio_ok = await _audio_from_durl(
                    client, bvid, cid, ffmpeg, a_file, seconds, headers
                )
            if await _merge(ffmpeg, v_file, a_file if audio_ok else "", out, seconds):
                logger.info(f"[bili_card] 备用地址下载成功：{_host(v_url)}")
                if not audio_ok:
                    logger.info("[bili_card] 没拿到声音，这条只有画面")
                return out, duration
    logger.info("[bili_card] 备用地址：几个节点都没下成")
    return "", 0.0

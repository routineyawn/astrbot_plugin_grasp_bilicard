"""文字：B 站字幕、本机/云端转写、简介、字幕质量校验与裁剪。
"""

import asyncio
import json
import os
import re
from astrbot.api import logger

from .http import BILI_UA, _bili_json, _bvid_of, cookie_header, page_cid, page_of
from .proc import run_cmd
from .security import bili_cookie


TAG_RE = re.compile(r"<\|[^|]*\|>")


NOISE_LINES = (
    "num threads:",
    "decoding method:",
    "Elapsed seconds:",
    "Real time factor",
    "Creating recognizer",
    "recognizer created",
    "OfflineRecognizerConfig",
    "Started",
    "Done!",
    "----",
)


SUB_LANGS = ["zh-Hans", "zh-CN", "zh", "ai-zh", "zh-TW", "zh-Hant"]
SUB_STAMP = re.compile(r"^\s*\[(\d+):(\d{2})\]\s*(.*)$")


SUBTITLE_WHY = {
    "mismatch": "字幕覆盖时长对不上",
    "wrong_lang": "字幕看着像别的视频",
    "empty": "没有字幕",
    "timeout": "字幕接口超时",
    "no_view": "查不到视频信息",
    "no_bvid": "认不出视频号",
    "no_httpx": "没装 httpx",
    "off": "关掉了字幕优先",
}
def clean_text(text: str) -> str:
    text = TAG_RE.sub("", text or "")
    return re.sub(r"\s+", " ", text).strip()


FILLER_RE = re.compile(r"[\s，。、,.!?！？…~～\-—:：;；\"'“”‘’()（）]+")


def speech_ok(heard: str) -> bool:
    """转写结果像不像一句真话。

    纯音乐、环境音、只有「嗯/啊」这类语气词时，sherpa-onnx 也会吐出几个字；
    直接注进去除了浪费上下文没有别的作用。低于 8 个字、或者只有一两个字
    反复循环的，一律当成没听到内容。
    """
    core = FILLER_RE.sub("", heard or "")
    if len(core) < 8:
        return False
    if len(set(core)) <= 2:
        return False
    return True


def parse_asr(raw: str) -> str:
    words = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # sherpa 的 SenseVoice 输出是一行 JSON，正文在 text 里
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("text"):
                    words.append(str(obj["text"]).strip())
            except Exception:  # noqa: BLE001
                pass
            continue
        if line.startswith(NOISE_LINES):
            continue
        if re.match(r"^[A-Za-z]:[\\/]", line) or line.lower().endswith(".wav"):
            continue
        m = re.match(r"^\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*[:：]\s*(.+)$", line)
        if m:
            words.append(m.group(1).strip())
            continue
        if re.search(r"(sherpa-onnx|loading|error|onnxruntime|usage|failed)", line, re.I):
            continue
        words.append(line)
    return clean_text(" ".join(w for w in words if w))


def trim_heard(text: str, max_seconds: int, duration: float) -> str:
    """把字幕裁到和画面同一个时间段。

    带 [mm:ss] 时间轴的字幕按时间裁；纯文本没有时间轴，只能按字数比例估。
    """
    text = (text or "").strip()
    if not text:
        return ""
    lines = [x for x in text.splitlines() if x.strip()]
    stamps = [SUB_STAMP.match(x) for x in lines]
    if lines and all(stamps):
        kept = [
            m.group(0).strip()
            for m in stamps
            if int(m.group(1)) * 60 + int(m.group(2)) <= max_seconds
        ]
        return "\n".join(kept)
    if duration > 0 and max_seconds < duration:
        keep = max(200, int(len(text) * max_seconds / duration))
        if keep < len(text):
            return text[:keep].rstrip() + "…（后面没看）"
    return text


def _merge_lines(pieces: list[str]) -> str:
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if piece and (not out or out[-1] != piece):
            out.append(piece)
    return "".join(out)


def parse_subtitle(path: str) -> str:
    """json3 / srt / vtt 字幕都转成纯文本。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    if not raw.strip():
        return ""
    pieces: list[str] = []
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and isinstance(obj.get("events"), list):
            for ev in obj["events"]:
                for seg in ev.get("segs") or []:
                    text = seg.get("utf8") if isinstance(seg, dict) else ""
                    if text:
                        pieces.append(str(text))
            return _merge_lines(pieces)
    except (json.JSONDecodeError, TypeError):
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        if line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        pieces.append(line)
    return _merge_lines(pieces)


async def fetch_desc(url: str, cookie: str) -> str:
    """视频简介：UP 主自己写的说明，通常最能说明这条视频想讲什么。"""
    try:
        import httpx
    except ImportError:
        return ""
    ck = cookie_header(bili_cookie(url, cookie))
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if ck:
        headers["Cookie"] = ck
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            bvid = await _bvid_of(url, headers)
            if not bvid:
                return ""
            data = await _bili_json(
                client,
                "https://api.bilibili.com/x/web-interface/view",
                {"bvid": bvid},
                headers,
            )
            desc = (((data or {}).get("data") or {}).get("desc") or "").strip()
            return desc[:300]
    except Exception:  # noqa: BLE001
        return ""


def title_grams(title: str) -> set[str]:
    """把标题拆成两字词，用来判断字幕是不是这个视频的。"""
    out: set[str] = set()
    for seg in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", title or ""):
        for i in range(len(seg) - 1):
            out.add(seg[i : i + 2])
    return out


def subtitle_problem(title: str, text: str, dur: int, max_end: float) -> str:
    """字幕看起来不是这个视频的，返回原因；能用就返回空串。"""
    if dur > 1 and max_end > 0:
        ratio = max_end / dur
        if ratio < 0.85 or ratio > 1.15:
            return "覆盖时长对不上"
    if len(text) < 150:
        return "内容太短"
    grams = title_grams(title)
    if grams and sum(1 for g in grams if g in text[:1000]) < 2:
        return "和标题对不上"
    return ""


# 这两类判据只是弱证据：标注不全也会命中，不一定是别人的字幕，留作备选。
WEAK_PROBLEMS = ("和标题对不上", "内容太短")


def weak_problem(problem: str) -> bool:
    return bool(problem) and problem.startswith(WEAK_PROBLEMS)


async def fetch_bili_subtitle(url: str, cookie: str) -> tuple[str, str]:
    """直接问 B 站要字幕，返回 (带 [mm:ss] 的文本, 状态)。

    优先按 aid/cid 查 x/v2/subtitle/web/view，失败再回退 x/player/v2；
    cue 时间轴明显超出视频时长就判为串片丢弃。
    """
    try:
        import httpx
    except ImportError:
        return "", "no_httpx"
    ck = cookie_header(bili_cookie(url, cookie))
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if ck:
        headers["Cookie"] = ck
    async with httpx.AsyncClient(timeout=12) as client:
        bvid = await _bvid_of(url, headers)
        if not bvid:
            return "", "no_bvid"
        view = await _bili_json(
            client,
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": bvid},
            headers,
        )
        data = view.get("data") or {}
        page = page_of(url)
        cid, dur = page_cid(data, page)
        title = str(data.get("title") or "")
        if not cid:
            return "", "no_view"
        payload = await _bili_json(
            client,
            "https://api.bilibili.com/x/player/v2",
            {"bvid": bvid, "cid": cid},
            headers,
        )
        subs = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return "", "empty"
        # 中文轨排前面；一条轨拿不到就换下一条，最多试三条。
        ordered = sorted(
            subs,
            key=lambda s: 0 if str(s.get("lan") or "").lower().startswith(("zh", "ai-zh")) else 1,
        )
        tried = 0
        last = "empty"
        weak = ""
        for pick in ordered:
            sub_url = str((pick or {}).get("subtitle_url") or "")
            if not sub_url:
                continue
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            tried += 1
            body = await _bili_json(client, sub_url, None, headers)
            lines: list[str] = []
            ends: list[float] = []
            for item in body.get("body") or []:
                text = (item.get("content") or "").strip()
                if not text:
                    continue
                try:
                    start = max(0.0, float(item.get("from") or 0))
                    end = max(start, float(item.get("to") or start))
                except (TypeError, ValueError):
                    start = end = 0.0
                ends.append(end)
                lines.append(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] {text}")
            if not lines:
                if tried >= 3:
                    break
                continue
            # 串片：这条轨不是这个视频的，换下一条试，不直接放弃。
            if dur and max(ends) > dur * 1.5:
                last = "mismatch"
                if tried >= 3:
                    break
                continue
            # 中文轨却几乎没中文：CDN 有时候会把别的视频的字幕给过来。
            if str(pick.get("lan") or "").lower().startswith(("zh", "ai-zh")):
                body_text = "".join(x.get("content") or "" for x in (body.get("body") or []))
                cn = sum(1 for ch in body_text if "\u4e00" <= ch <= "\u9fff")
                if body_text and cn / len(body_text) < 0.15:
                    last = "wrong_lang"
                    if tried >= 3:
                        break
                    continue
            problem = subtitle_problem(title, "".join(lines), dur, max(ends))
            if problem:
                last = problem
                logger.info(f"[bili_card] 这份字幕不能用：{problem}（{len(lines)} 行）")
                if weak_problem(problem):
                    now = "\n".join(lines)
                    if len(now) > len(weak):
                        weak = now
                if tried >= 3:
                    break
                continue
            return "\n".join(lines), "ok"
        # 弱证据判据全失败时，用最长的一条兜底：比为了「严格」去跑十几秒音频转写划算。
        if len(weak) >= 200:
            logger.info(f"[bili_card] 字幕校验都没过，用最长的一条兜底（{len(weak)} 字）")
            return weak, "weak"
        return "", last


async def fetch_subtitle(url: str, work: str, cookie: str) -> tuple[str, str]:
    """有 B 站 cookie 时先问字幕接口，拿不到再用 yt-dlp 兜底。

    返回 (文字, 状态)：状态 ok 表示拿到了，其余是没拿到的原因。
    """
    why = ""
    cookie = bili_cookie(url, cookie)
    if cookie_header(cookie):
        status = ""
        for attempt in range(2):
            # 字幕接口偶尔会被 B 站限流卡住，给整次请求设上限，别拖住整条处理
            try:
                text, status = await asyncio.wait_for(
                    fetch_bili_subtitle(url, cookie), timeout=10
                )
            except asyncio.TimeoutError:
                text, status = "", "timeout"
            if text:
                logger.info(f"[bili_card] B 站字幕接口拿到 {len(text)} 字")
                return text, "ok"
            why = status or why
            # 只有「可能是一时的」失败才重试：校验没过是确定性的，重试也是同样的结果。
            if attempt == 0 and status in ("timeout", "empty"):
                await asyncio.sleep(0.4)
        logger.info(f"[bili_card] B 站字幕接口没拿到（{status}），试 yt-dlp")
    else:
        logger.info("[bili_card] 没填 B 站 Cookie，跳过字幕接口（匿名基本拿不到）")
        why = why or "empty"
    try:
        import yt_dlp
    except ImportError:
        return "", "no_ytdlp"
    os.makedirs(work, exist_ok=True)

    def go():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": SUB_LANGS,
            "subtitlesformat": "json3/srt/vtt/best",
            "outtmpl": os.path.join(work, "sub"),
        }
        ck = cookie_header(cookie)
        if ck:
            opts["http_headers"] = {
                "Cookie": ck,
                "Referer": "https://www.bilibili.com/",
            }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.to_thread(go)
    except Exception as e:  # noqa: BLE001
        logger.info(f"[bili_card] 没拿到字幕：{e}")
        return "", why or "empty"
    exts = ("json3", "srt", "vtt", "srv3")
    for name in sorted(os.listdir(work)):
        if not name.startswith("sub.") or name.rsplit(".", 1)[-1] not in exts:
            continue
        text = parse_subtitle(os.path.join(work, name))
        if text:
            return text, "ok"
    return "", why or "empty"


def find_asr(asr_dir: str) -> tuple[str, str, str]:
    """返回 (可执行文件, 模型, tokens)；缺一个就返回空串。"""
    asr_dir = (asr_dir or "").strip().strip('"')
    if not asr_dir or not os.path.isdir(asr_dir):
        return "", "", ""
    exe = os.path.join(asr_dir, "sherpa-onnx-offline.exe" if os.name == "nt" else "sherpa-onnx-offline")
    if not os.path.isfile(exe):
        return "", "", ""
    model = os.path.join(asr_dir, "model.int8.onnx")
    if not os.path.isfile(model):
        cands = [f for f in os.listdir(asr_dir) if f.endswith(".onnx")]
        model = os.path.join(asr_dir, cands[0]) if cands else ""
    tokens = os.path.join(asr_dir, "tokens.txt")
    if not model or not os.path.isfile(tokens):
        return "", "", ""
    return exe, model, tokens


async def transcribe_local(exe: str, model: str, tokens: str, wavs: list[str]) -> str:
    pieces = []
    for wav in wavs:
        code, out = await run_cmd(
            [
                exe,
                f"--sense-voice-model={model}",
                f"--tokens={tokens}",
                "--sense-voice-language=zh",
                "--sense-voice-use-itn=true",
                wav,
            ],
            120,
        )
        text = parse_asr(out)
        if text:
            pieces.append(text)
    return clean_text("".join(pieces))


async def transcribe_api(wavs: list[str], base: str, key: str, model: str) -> str:
    """任何 OpenAI 兼容的 /audio/transcriptions 服务都能用（硅基流动、OpenAI、Groq、本机 faster-whisper 等）。"""
    try:
        import httpx
    except ImportError:
        logger.warning("[bili_card] 没装 httpx，跳过云转写")
        return ""
    api = (base or "https://api.siliconflow.cn/v1").rstrip("/") + "/audio/transcriptions"
    pieces = []
    for wav in wavs:
        try:
            with open(wav, "rb") as f:
                data = f.read()
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    api,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (os.path.basename(wav), data, "audio/wav")},
                    data={"model": model},
                )
            resp.raise_for_status()
            obj = resp.json()
            if isinstance(obj, dict) and obj.get("text"):
                pieces.append(str(obj["text"]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_card] 云转写失败：{e}")
            break
    return clean_text(" ".join(pieces))

def hot_comments(comments: list[str], limit: int, chars: int) -> str:
    """把几条热评拼成一行，带一句「别全信」的提醒。

    评论是别人写的，可能玩梗、剧透、说错；拼的时候只取正文，不带昵称。
    """
    n = max(0, int(limit))
    cap = max(20, int(chars))
    picked = []
    for raw in comments or []:
        body = " ".join(str(raw or "").split())
        if not body:
            continue
        picked.append(body[:cap])
        if len(picked) >= n:
            break
    if not picked or n <= 0:
        return ""
    body = "」「".join(picked)
    return f"评论区（别人说的，不一定对，和画面冲突时以画面为准）：「{body}」。\n"


async def fetch_meta(url: str, cookie: str) -> tuple[str, int]:
    """取分区、标签和 aid：给模型一个「这是哪一区的视频」的定调。

    返回 (说明文字, aid)；aid 拿不到就是 0，热评那边要靠它。
    """
    try:
        import httpx
    except ImportError:
        return "", 0
    ck = cookie_header(bili_cookie(url, cookie))
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if ck:
        headers["Cookie"] = ck
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            bvid = await _bvid_of(url, headers)
            if not bvid:
                return "", 0
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/view/detail",
                params={"bvid": bvid},
                headers=headers,
            )
            if resp.status_code != 200:
                return "", 0
            obj = resp.json()
    except Exception:  # noqa: BLE001
        return "", 0
    data = (obj.get("data") or {}) if isinstance(obj, dict) else {}
    view = data.get("View") or {}
    try:
        aid = int(view.get("aid") or 0)
    except (TypeError, ValueError):
        aid = 0
    tname = str(view.get("tname") or "").strip()
    tags: list[str] = []
    for item in data.get("Tags") or []:
        name = str((item or {}).get("tag_name") or "").strip()
        if name and name not in tags:
            tags.append(name)
    bits = []
    if tname:
        bits.append(f"分区 {tname}")
    if tags:
        bits.append("标签 " + "、".join(tags[:8]))
    return ("，".join(bits), aid)


async def fetch_comments(cookie: str, aid: int, want: int = 20) -> list[str]:
    """按点赞排的热评正文，从多到少；只留文本，不留昵称。"""
    if not aid:
        return []
    try:
        import httpx
    except ImportError:
        return []
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    ck = cookie_header(cookie)
    if ck:
        headers["Cookie"] = ck
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.bilibili.com/x/v2/reply",
                params={"type": 1, "oid": int(aid), "sort": 2, "ps": max(1, int(want)), "pn": 1},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            obj = resp.json()
    except Exception:  # noqa: BLE001
        return []
    data = (obj.get("data") or {}) if isinstance(obj, dict) else {}
    out: list[str] = []
    for item in data.get("replies") or []:
        message = str(((item or {}).get("content") or {}).get("message") or "")
        message = " ".join(message.split())
        if message and message not in out:
            out.append(message)
    return out


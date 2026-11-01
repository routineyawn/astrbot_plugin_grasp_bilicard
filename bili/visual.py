"""画面：低清扫描、按变化分段、挑帧、拼宫格、官方故事板兜底。
"""

import asyncio
import math
import os
from astrbot.api import logger

from .http import BILI_UA, _bili_json, _bvid_of, page_cid, page_of
from .proc import mmss, run_cmd, run_cmd_bytes


# 宫格：整图面积预算。640k 像素下 9 格每格约 350×194，够认出人和动作；
# 每格再小就只剩色块，所以格数由"每格至少 MIN_CELL_*"反推，不由配置直接决定。
BOARD_BUDGET = 640_000
BOARD_GUTTER = 4
BOARD_BG = (18, 18, 20)
BOARD_PAD = (42, 42, 46)
MIN_CELL_W, MIN_CELL_H = 240, 135


# 扫画面变化用的低清尺寸：64×36 已经足够看出"这里换镜头了"，还省解码开销
SCAN_W, SCAN_H = 64, 36
SCAN_FPS = 4


def _rgb_to_hsv(rgb):
    """(N,H,W,3) 的 0..1 RGB → 同形状 HSV（色相/饱和/明度都归一 0..1），全 numpy。"""
    import numpy as np

    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    d = mx - mn
    safe = np.where(d > 1e-6, d, 1.0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    h = np.zeros_like(mx)
    m1 = mx == r
    m2 = (mx == g) & ~m1
    m3 = (mx == b) & ~m1 & ~m2
    h[m1] = ((g - b) / safe)[m1] % 6.0
    h[m2] = ((b - r) / safe)[m2] + 2.0
    h[m3] = ((r - g) / safe)[m3] + 4.0
    h = h / 6.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return np.stack([h, s, mx], axis=-1)


async def scan_motion(
    ffmpeg: str, path: str, duration: float, max_seconds: int, fps: int = SCAN_FPS
) -> list[tuple[float, float]]:
    """按 fps 把前 max_seconds 秒缩到 64×36，返回 [(时间, 0-255 画面变化分数)]。

    分数 = 三通道差均值；色相差取环形距离，并按两帧较低饱和度降权
    （灰白画面里色相抖动没有意义，不该被算成"换镜头"）。
    """
    span = min(duration, float(max_seconds)) if duration > 0 else float(max_seconds)
    if span <= 0.5:
        return []
    code, raw = await run_cmd_bytes(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-t", f"{span:.2f}", "-i", path,
            "-vf", f"fps={max(1, int(fps))},scale={SCAN_W}:{SCAN_H}:flags=area",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
    )
    if code != 0 or not raw:
        return []
    import numpy as np

    size = SCAN_W * SCAN_H * 3
    n = len(raw) // size
    if n < 2:
        return []
    arr = np.frombuffer(raw[: n * size], dtype=np.uint8).reshape(n, SCAN_H, SCAN_W, 3).astype(np.float32) / 255.0
    hsv = _rgb_to_hsv(arr)
    dh = np.abs(hsv[1:, :, :, 0] - hsv[:-1, :, :, 0])
    dh = np.minimum(dh, 1.0 - dh)
    hw = np.minimum(hsv[1:, :, :, 1], hsv[:-1, :, :, 1])
    ds = np.abs(hsv[1:, :, :, 1] - hsv[:-1, :, :, 1])
    dv = np.abs(hsv[1:, :, :, 2] - hsv[:-1, :, :, 2])
    score = (dh * hw + ds + dv).mean(axis=(1, 2)) * 255.0
    return [((i + 1) / max(1, int(fps)), float(v)) for i, v in enumerate(score)]


def segment_by_motion(scores: list[tuple[float, float]], duration: float, cells: int) -> list[tuple[float, float, float]]:
    """按累计画面变化量把时间轴等分成 cells 段，返回 [(起, 止, 段内总变化量)]。

    不用"切点计数"当密度：画面变化率是连续信号，不依赖切点判得准不准；
    变化大的段自然更短，同一张宫格里每格覆盖的"信息量"才大致相当。
    """
    if not scores or cells < 1:
        return []
    times = [t for t, _ in scores]
    vals = [max(0.0, v) for _, v in scores]
    total = sum(vals)
    end = float(duration) if duration > 0 else (times[-1] if times else 0.0)
    if total <= 1e-6:
        step = end / cells
        return [(round(i * step, 2), round(min(end, (i + 1) * step), 2), 0.0) for i in range(cells)]
    cum = []
    acc = 0.0
    for v in vals:
        acc += v
        cum.append(acc)
    edges = [0.0]
    for i in range(1, cells):
        target = total * i / cells
        idx = next((k for k, c in enumerate(cum) if c >= target), len(cum) - 1)
        t = times[idx] if idx < len(times) else end
        edges.append(max(edges[-1], min(end, t)))
    edges.append(end)
    out = []
    for i in range(cells):
        a, b = edges[i], edges[i + 1]
        if b - a < 0.2:
            b = min(end, a + 0.2)
        out.append((round(a, 2), round(b, 2), 0.0))
    return out


def board_layout(
    cells: int, aspect: float = 16 / 9, budget: int = BOARD_BUDGET
) -> tuple[int, int, int, int, int, int]:
    """格数 → (列, 行, 画布宽, 画布高, 格宽, 格高)。整图面积不超过 budget（默认 BOARD_BUDGET）。"""
    import math

    want = max(1, min(16, int(cells)))
    best = None
    for cols in range(1, want + 1):
        rows = (want + cols - 1) // cols
        ca = (cols / rows) * aspect
        cost = abs(math.log(ca / aspect)) + 0.15 * (cols * rows - want)
        key = (round(cost, 6), cols * rows)
        if best is None or key < best[0]:
            best = (key, cols, rows)
    _, cols, rows = best
    ca = (cols / rows) * aspect
    gw = math.sqrt(max(60_000, int(budget)) * ca)
    gh = gw / ca
    cw = max(1, int((gw - BOARD_GUTTER * (cols + 1)) // cols))
    ch = max(1, int((gh - BOARD_GUTTER * (rows + 1)) // rows))
    w = cols * cw + BOARD_GUTTER * (cols + 1)
    h = rows * ch + BOARD_GUTTER * (rows + 1)
    return cols, rows, w, h, cw, ch


def fit_board_cells(want: int, aspect: float = 16 / 9, budget: int = BOARD_BUDGET) -> int:
    """在想要的格数以内，挑一个每格至少 MIN_CELL 的格数；都不行就 1 格。"""
    cap = max(1, min(16, int(want)))
    for n in [cap, 12, 9, 6, 4, 2, 1]:
        if n > cap:
            continue
        _, _, _, _, cw, ch = board_layout(n, aspect, budget)
        if cw >= MIN_CELL_W and ch >= MIN_CELL_H:
            return n
    return 1


def compose_board(
    frames: list[tuple[float, str]], out_path: str, cells: int, budget: int = BOARD_BUDGET
) -> bool:
    """把若干帧拼成一张宫格，每格左上角烧编号。不依赖字体文件，用 Pillow 自带位图字体。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    cols, rows, w, h, cw, ch = board_layout(cells, 16 / 9, budget)
    canvas = Image.new("RGB", (w, h), BOARD_BG)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=max(10, int(ch * 0.22)))
    except TypeError:
        font = ImageFont.load_default()
    for i, (_t, p) in enumerate(frames):
        if i >= cols * rows:
            break
        r, c = divmod(i, cols)
        x = BOARD_GUTTER + c * (cw + BOARD_GUTTER)
        y = BOARD_GUTTER + r * (ch + BOARD_GUTTER)
        try:
            im = Image.open(p).convert("RGB")
        except Exception:  # noqa: BLE001
            canvas.paste(Image.new("RGB", (cw, ch), BOARD_PAD), (x, y))
            continue
        sw, sh = im.size
        if sw / sh > cw / ch:
            nw = max(1, int(sh * cw / ch))
            im = im.crop(((sw - nw) // 2, 0, (sw + nw) // 2, sh))
        else:
            nh = max(1, int(sw * ch / cw))
            im = im.crop((0, (sh - nh) // 2, sw, (sh + nh) // 2))
        canvas.paste(im.resize((cw, ch), Image.LANCZOS), (x, y))
        label = str(i + 1)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
        pad = 3
        draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2], fill=(0, 0, 0))
        draw.text((x + pad, y + pad), label, fill=(255, 235, 59), font=font)
    try:
        canvas.save(out_path, quality=88)
        return True
    except Exception:  # noqa: BLE001
        return False


def sample_times(duration: float, limit: int, count: int) -> list[float]:
    """在有效的观看区间里均匀取几个时间点。"""
    span = min(duration, float(limit)) if duration > 0 else float(limit)
    if span <= 0:
        span = float(limit)
    n = max(1, int(count))
    lo, hi = span * 0.1, span * 0.9
    if n == 1:
        return [round(span * 0.5, 2)]
    return [round(lo + (hi - lo) * i / (n - 1), 2) for i in range(n)]


async def extract_frames(
    ffmpeg: str, video: str, seconds: list[float], dest: str
) -> list[tuple[float, str]]:
    """每个时间点截一张 jpg；截不出来的那一点直接跳过。"""
    os.makedirs(dest, exist_ok=True)
    frames = []
    for idx, t in enumerate(seconds):
        out = os.path.join(dest, f"f{idx:02d}.jpg")
        code, _ = await run_cmd(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", video,
                "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                "-q:v", "3", "-y", out,
            ],
            60,
        )
        if code == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            frames.append((t, out))
    return frames


def frame_sharpness(path: str) -> float:
    """拉普拉斯方差，越大越清晰。缺 PIL/numpy 返回 0，等于不挑。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return 0.0
    try:
        with Image.open(path) as im:
            g = im.convert("L").resize((160, 90))
            a = np.asarray(g, dtype=np.float32)
    except Exception:  # noqa: BLE001
        return 0.0
    if a.size < 9:
        return 0.0
    lap = np.abs(
        a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    )
    return float(lap.var())


def pick_cells(scores: list[tuple[float, float]], want: int) -> int:
    """按画面起伏决定格数：平缓的降到 6 格，中等 9 格，剧烈的用满 want。"""
    want = max(1, want)
    if not scores or want <= 1:
        return want
    vals = sorted(s for _, s in scores)
    n = len(vals)
    med = max(vals[n // 2], 0.5)
    p90 = vals[min(n - 1, int(n * 0.9))]
    ratio = p90 / med
    if ratio < 2.0:
        return max(1, min(want, 6))
    if ratio < 4.0:
        return max(1, min(want, 9))
    return want


async def pick_sharp_frame(
    ffmpeg: str, path: str, start: float, end: float, out_dir: str, idx: int, sharp: bool = True
) -> tuple[float, str] | None:
    """一段里取几个候选点，挑最清晰的一张，避开转场和运动模糊。"""
    span = max(end - start, 0.2)
    if sharp:
        points = [
            round(start + span * 0.25, 2),
            round((start + end) / 2, 2),
            round(end - span * 0.25, 2),
        ]
    else:
        points = [round((start + end) / 2, 2)]
    best: tuple[float, float, str] | None = None
    for i, t in enumerate(points):
        got = await extract_frames(ffmpeg, path, [t], os.path.join(out_dir, f"c{idx:02d}_{i}"))
        if not got:
            continue
        t2, p = got[0]
        score = frame_sharpness(p)
        if best is None or score > best[0]:
            if best:
                try:
                    os.remove(best[2])
                except OSError:
                    pass
            best = (score, t2, p)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    if not best:
        return None
    return best[1], best[2]


def image_tokens(width: int, height: int) -> int:
    """按 42px 一块的粗口径估图要多少 token，只作数量级参考。"""
    return int(math.ceil(width / 42)) * (int(math.ceil(height / 42)) + 1) + 2


async def make_visual(
    ffmpeg: str, path: str, duration: float, max_seconds: int, conf: dict, work: str
) -> dict:
    """把视频变成「这一轮能看的画面」：宫格一张，或逐帧几张。

    这是唯一的画面接缝：以后想换别的压缩方式，只改这一个函数。
    """
    style = (conf.get("frame_style") or "board").strip().lower()
    budget = int(conf.get("board_budget") or BOARD_BUDGET)
    out_dir = os.path.join(work, "frames")
    frames: list[tuple[float, str]] = []
    segs: list[tuple[float, float, float]] = []
    board_img = ""
    if style == "board":
        want = max(1, min(16, int(conf.get("board_cells", 9) or 9)))
        cells = fit_board_cells(want, 16 / 9, budget)
        sharp = bool(conf.get("pick_sharp", True))
        scores = await scan_motion(
            ffmpeg, path, duration, max_seconds, int(conf.get("scan_fps", SCAN_FPS) or SCAN_FPS)
        )
        if scores:
            # 起伏大就给更多格，平缓的省成 6 格
            cells = fit_board_cells(pick_cells(scores, cells), 16 / 9, budget)
            segs = segment_by_motion(scores, min(duration or max_seconds, max_seconds), cells)
            sem = asyncio.Semaphore(4)

            async def one(i: int, a: float, b: float):
                async with sem:
                    return await pick_sharp_frame(ffmpeg, path, a, b, out_dir, i, sharp)

            parts = await asyncio.gather(*(one(i, a, b) for i, (a, b, _) in enumerate(segs)))
            frames = [p for p in parts if p]
        if frames:
            out = os.path.join(work, f"board{len(frames)}.jpg")
            if compose_board(frames, out, len(frames), budget):
                board_img = out
            else:
                logger.info("[bili_card] 拼图失败，这轮退回逐帧")
    if not board_img:
        # 宫格做不出来（没 numpy/Pillow、扫描失败）就退回逐帧，功能不降级成"看不了"
        max_frames = max(1, int(conf.get("frame_count", 6) or 6))
        times = sample_times(duration, max_seconds, max_frames)
        frames = await extract_frames(ffmpeg, path, times, out_dir)
        segs = []
    images = [board_img] if board_img else [p for _, p in frames]
    if board_img:
        cols, rows, w, h, _cw, _ch = board_layout(len(frames), 16 / 9, budget)
        plan = "\n".join(f"{i + 1}：{mmss(a)}–{mmss(b)}" for i, (a, b, _c) in enumerate(segs))
        hint = (
            f"下面这 1 张图是 {cols}×{rows} 的宫格（共 {len(frames)} 格），每格左上角有编号，按时间顺序排；"
            f"每格对应的片段是：\n{plan}\n"
        )
        tokens = image_tokens(w, h)
    else:
        stamps = "、".join(mmss(t) for t, _ in frames)
        hint = f"下面 {len(frames)} 张图是从视频里按时间顺序截的画面（分别在 {stamps}）。"
        tokens = image_tokens(1280, 720) * len(frames)
    return {
        "images": images,
        "hint": hint,
        "frames": len(frames),
        "board": bool(board_img),
        "tokens": tokens,
    }


async def storyboard_visual(ffmpeg: str, url: str, work: str, conf: dict) -> dict:
    """下载失败时的兜底：B 站官方故事板（videoshot）的缩略图拼成宫格。

    官方故事板是 160×90 的小图、10×10 拼一张，比抽帧糊，但不需要下载视频，
    几十毫秒就能拿到，至少让模型有几张画面可看。
    """
    empty = {"images": [], "hint": "", "frames": 0, "board": False, "tokens": 0}
    try:
        import httpx
    except ImportError:
        return empty
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    want = fit_board_cells(int(conf.get("board_cells", 9) or 9))
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            bvid = await _bvid_of(url, headers)
            if not bvid:
                return empty
            view = await _bili_json(
                client,
                "https://api.bilibili.com/x/web-interface/view",
                {"bvid": bvid},
                headers,
            )
            data = view.get("data") or {}
            cid, page_dur = page_cid(data, page_of(url))
            duration = int(page_dur or 0)
            if not cid:
                return empty
            shot = await _bili_json(
                client,
                "https://api.bilibili.com/x/player/videoshot",
                {"bvid": bvid, "index": 1, "cid": cid},
                headers,
            )
            sd = shot.get("data") or {}
            urls = [str(u) for u in (sd.get("image") or []) if u]
            stamps = [float(x) for x in (sd.get("index") or [])]
            cols = max(1, int(sd.get("img_x_len") or 10))
            rows = max(1, int(sd.get("img_y_len") or 10))
            tw = max(1, int(sd.get("img_x_size") or 160))
            th = max(1, int(sd.get("img_y_size") or 90))
            if not urls or not stamps:
                return empty
            if duration and max(stamps) > duration * 1.5:
                stamps = [s / 1000.0 for s in stamps]
            u = urls[0]
            if u.startswith("//"):
                u = "https:" + u
            resp = await client.get(u, headers=headers)
            if resp.status_code != 200 or not resp.content:
                return empty
            os.makedirs(work, exist_ok=True)
            sheet = os.path.join(work, "sprite.jpg")
            with open(sheet, "wb") as f:
                f.write(resp.content)
    except Exception as e:  # noqa: BLE001
        logger.info(f"[bili_card] 故事板没取到：{e}")
        return empty
    try:
        from PIL import Image
    except ImportError:
        return empty
    total = min(len(stamps), cols * rows)
    if total <= 0:
        return empty
    pick_n = max(1, min(want, total))
    picks: list[int] = []
    for i in range(pick_n):
        pi = int(round(i * (total - 1) / max(pick_n - 1, 1)))
        if pi not in picks:
            picks.append(pi)
    frames: list[tuple[float, str]] = []
    try:
        with Image.open(sheet) as im:
            for n, pi in enumerate(picks):
                cx, cy = pi % cols, pi // cols
                cell = im.crop((cx * tw, cy * th, (cx + 1) * tw, (cy + 1) * th)).convert("RGB")
                cell = cell.resize((tw * 2, th * 2), Image.LANCZOS)
                cell_path = os.path.join(work, f"sb{n:02d}.jpg")
                cell.save(cell_path, quality=90)
                frames.append((stamps[pi] if pi < len(stamps) else 0.0, cell_path))
    except Exception as e:  # noqa: BLE001
        logger.info(f"[bili_card] 故事板裁图失败：{e}")
        return empty
    board_path = os.path.join(work, "storyboard.jpg")
    if not compose_board(frames, board_path, len(frames)):
        return empty
    _, _, w, h, _, _ = board_layout(len(frames))
    stamps_text = "\n".join(f"{i + 1}：{mmss(t)}" for i, (t, _) in enumerate(frames))
    hint = (
        "视频没下载成功，下面这张是 B 站官方故事板缩略图（比抽帧糊），"
        f"共 {len(frames)} 格，每格左上角有编号：\n{stamps_text}"
    )
    return {
        "images": [board_path],
        "hint": hint,
        "frames": len(frames),
        "board": True,
        "tokens": image_tokens(w, h),
    }


async def grab_audio(ffmpeg: str, path: str, max_seconds: int, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    wavs = []
    start = 0
    while start < max_seconds:
        part = min(30, max_seconds - start)
        out = os.path.join(out_dir, f"a{start:04d}.wav")
        code, _ = await run_cmd(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-t", str(part), "-i", path,
                "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "-y", out,
            ],
            90,
        )
        if code == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000:
            wavs.append(out)
        start += part
    return wavs

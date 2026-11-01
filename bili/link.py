"""链接识别：从 QQ 卡片、引用、纯文本里认出 B 站视频，并归一成同一个键。

只做字符串和 URL 判断，不发请求、不碰文件 —— 所以这一层是纯函数，能被测试完全覆盖。
"""

import json
import re
import time
from typing import Any
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Json, Reply
from .security import BILI_MARKS, host_in


# 直接写出来的 BV / av 号（没带链接）
RAW_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10}|av\d{1,12})(?![0-9A-Za-z])", re.I)
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)


def _first_link(text: str, marks: tuple[str, ...] = BILI_MARKS) -> str:
    if not text:
        return ""
    for m in URL_RE.finditer(text):
        url = m.group(0)
        if host_in(url, marks):
            return url
    return ""


def raw_link(event: AstrMessageEvent) -> str:
    """消息里直接写的 BV 号或 av 号也认，省得对方再贴一次链接。"""
    text = getattr(event, "message_str", "") or ""
    m = RAW_RE.search(text)
    if not m:
        return ""
    code = m.group(1)
    if code.lower().startswith("bv"):
        return "https://www.bilibili.com/video/" + code
    return "https://www.bilibili.com/video/" + code.lower()


def share_link(comp: Any, marks: tuple[str, ...] = BILI_MARKS) -> str:
    """从 Json 消息段里找出分享链接（默认只认 B 站）。"""
    if not isinstance(comp, Json):
        return ""
    data = getattr(comp, "data", None)
    if not isinstance(data, dict):
        return ""
    meta = data.get("meta")
    detail = meta.get("detail_1") if isinstance(meta, dict) else None
    if isinstance(detail, dict):
        for key in ("qqdocurl", "jumpUrl", "url"):
            val = detail.get(key)
            if isinstance(val, str):
                url = _first_link(val, marks)
                if url:
                    return url
    try:
        return _first_link(json.dumps(data, ensure_ascii=False), marks)
    except Exception:  # noqa: BLE001
        return ""


def share_title(comp: Any) -> str:
    """卡片上的标题（一般是 UP 主的视频标题）或提示语。"""
    if not isinstance(comp, Json):
        return ""
    data = getattr(comp, "data", None)
    if not isinstance(data, dict):
        return ""
    meta = data.get("meta")
    detail = meta.get("detail_1") if isinstance(meta, dict) else None
    for src in (detail, data):
        if isinstance(src, dict):
            for key in ("desc", "title", "prompt"):
                val = src.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:120]
    return ""


def url_key(url: str) -> str:
    """把分享链接归一成"这是哪条视频"：剥掉 ts/bbid 这类查询参数，
    b23 短链和完整 BV 链都能落到同一个键上。"""
    raw = (url or "").strip()
    pm = re.search(r"[?&]p=(\d+)", raw)
    page = f":p{max(1, int(pm.group(1)))}" if pm else ""
    u = raw.split("?")[0].split("#")[0].rstrip("/").lower()
    m = re.search(r"(bv[0-9a-z]{10})", u)
    if m:
        return "bv:" + m.group(1) + page
    m = re.search(r"(?:b23\.tv|bili2233\.cn)/([0-9a-z]+)", u)
    if m:
        return "b23:" + m.group(1) + page
    return u


def same_sender(item: tuple, event: AstrMessageEvent) -> bool:
    """群里先发卡片、后 @ 的那条，得是同一个人发的才认这张卡片。"""
    if len(item) < 4 or not item[3]:
        return True
    try:
        sender = str(event.get_sender_id() or "")
    except Exception:
        return True
    return not sender or sender == item[3]


def chain_link(
    event: AstrMessageEvent,
    recent: dict | None = None,
    marks: tuple[str, ...] = BILI_MARKS,
    wait: float = 300,
) -> str:
    """当前消息链或引用消息里的分享链接，找不到再看这个会话最近收到的卡片。"""
    cached = getattr(event, "_bili_link", "")
    if cached:
        return cached
    msg = getattr(event, "message_obj", None)
    chain = (msg.message if msg else []) or []
    for comp in chain:
        url = share_link(comp, marks)
        if url:
            return url
    for comp in chain:
        if isinstance(comp, Reply):
            for sub in comp.chain or []:
                url = share_link(sub, marks)
                if url:
                    return url
    if recent:
        item = recent.get(getattr(event, "session_id", ""))
        if item and same_sender(item, event) and time.time() - item[2] < wait:
            return item[0]
    return raw_link(event)

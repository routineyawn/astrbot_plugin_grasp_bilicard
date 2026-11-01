"""B 站接口的公共部分：UA、取 JSON、从链接里认 BV 号、拼 Cookie 头。
"""

import re

from .security import bad_host, url_host


BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def _bili_json(client, url: str, params: dict | None, headers: dict) -> dict:
    try:
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            return {}
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


_BV_CACHE: dict[str, tuple[str, int]] = {}
_PAGE_CACHE: dict[str, int] = {}


def bv_cache_key(url: str) -> str:
    m = re.search(r"(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]+)", url or "")
    return ("b23:" + m.group(1).lower()) if m else ""


def page_of(url: str) -> int:
    """链接指向第几个分 P，没有就是 P1。

    短链本身不带 p，要等 _bvid_of 跟随重定向、把最终地址里的 p 记进
    _PAGE_CACHE 之后才认得出；所以同一个 url 先调 _bvid_of 再调这个。
    """
    m = re.search(r"[?&]p=(\d+)", url or "")
    if m:
        return max(1, int(m.group(1)))
    return _PAGE_CACHE.get(bv_cache_key(url), 1)


def page_cid(data: dict, page: int) -> tuple[int, float]:
    """从 view 接口的 data 里取第 page 个分 P 的 cid 和时长。

    番剧、课程、合集都是多 P，一个 BV 下每个 P 有自己的 cid；取错 cid
    拿到的是第 1 话的流和字幕。pages 缺失时退回顶层 cid。
    """
    pages = data.get("pages")
    if isinstance(pages, list) and 0 < page <= len(pages):
        pg = pages[page - 1] or {}
        cid = int(pg.get("cid") or data.get("cid") or 0)
        dur = float(pg.get("duration") or data.get("duration") or 0)
        return cid, dur
    return int(data.get("cid") or 0), float(data.get("duration") or 0)


async def _bvid_of(url: str, headers: dict) -> str:
    """从链接里取 BV 号；短链（b23.tv / bili2233.cn）先跟随重定向，再退一步从网页里找。

    解析结果按短码缓存：同一条卡片会被预取和主流程各解析一次，
    盯着 b23 连打几次容易被判成爬虫，直接回 404「啥都木有」。
    """
    found = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if found:
        return found.group(0)
    ck = bv_cache_key(url)
    if ck and ck in _BV_CACHE:
        bv, page = _BV_CACHE[ck]
        # 分 P 号跟 BV 一起缓存：同一条短链永远指同一话，
        # 命中缓存时也要把 p 还原出来，否则会退回 P1
        _PAGE_CACHE[ck] = page
        return bv
    try:
        import httpx
    except ImportError:
        return ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8) as client:
            # 有的节点只对 GET 下发跳转，HEAD 直接返回 200 空页
            for method in ("head", "get"):
                try:
                    resp = await getattr(client, method)(url, headers=headers)
                except Exception:  # noqa: BLE001
                    continue
                final = str(resp.url)
                # 跟随重定向后可能落到内网或本机（短链被劫持、粘贴了别的地址），
                # 这里再拦一次：不改写请求，只拒绝继续用这个结果
                if bad_host(url_host(final)):
                    continue
                found = re.search(r"(BV[0-9A-Za-z]{10})", final) or re.search(
                    r"(BV[0-9A-Za-z]{10})", resp.text or ""
                )
                if found:
                    page = page_of(str(resp.url))
                    if ck:
                        _BV_CACHE[ck] = (found.group(0), page)
                        _PAGE_CACHE[ck] = page
                    return found.group(0)
        return ""
    except Exception:  # noqa: BLE001
        return ""


def cookie_header(raw: str) -> str:
    """只要填了 SESSDATA 的裸值，就自动补成完整 Cookie 头。"""
    c = (raw or "").strip()
    if not c:
        return ""
    if "=" not in c:
        return f"SESSDATA={c}"
    return c

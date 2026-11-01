"""域名与凭证边界：谁能被请求、Cookie 发给谁。

所有网络入口都必须先过这里 —— 这是 v0.1.1 那次 Cookie 外发事故留下的规矩。
"""

import re


# B 站短链域名：这些链接要先跟随重定向换成 BV，才能交给 yt-dlp
SHORT_HOSTS = ("b23.tv", "bili2233.cn")
BILI_MARKS = SHORT_HOSTS + ("bilibili.com",)


def bad_host(host: str) -> bool:
    """本机、内网、没有点号的地址不当站点：免得一条消息引着插件去请求自家服务。"""
    host = (host or "").strip().lower().strip(".")
    if not host or "." not in host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return True
    if host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        return False


def url_host(url: str) -> str:
    """取 URL 的主机名；解析不了、或不是 http(s) 协议就返回空串。"""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit((url or "").strip())
    except Exception:  # noqa: BLE001
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    return (parts.hostname or "").strip().lower().strip(".")


def host_in(url: str, marks: tuple[str, ...]) -> bool:
    """主机名严格匹配：等于该域名，或它的子域名。

    不能用子串判断——`https://evil.com/?x=b23.tv` 里含 `b23.tv`，
    子串写法会放行，之后 Cookie 就发到别人的机器上了。
    """
    host = url_host(url)
    if not host:
        return False
    for m in marks:
        k = (m or "").strip().lower().strip(".")
        if not k:
            continue
        if host == k or host.endswith("." + k):
            return True
    return False


def bili_cookie(url: str, cookie: str) -> str:
    """Cookie 只发给真正的 B 站域名；extra_sites 里加的站点一律不带。"""
    return cookie if host_in(url, BILI_MARKS) else ""


def site_marks(extra: str = "") -> tuple[str, ...]:
    """默认只认 B 站；配置里额外写的域名也一起认（逗号或空格分隔）。

    内网地址、本机地址一律不收。
    """
    items = []
    for x in re.split(r"[,，\s]+", extra or ""):
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", x.strip().lower())
        host = host.split("/")[0].split("?")[0].split("#")[0].strip().strip(".")
        if not host or bad_host(host):
            continue
        items.append(host)
    return tuple(dict.fromkeys(BILI_MARKS + tuple(items)))

"""纯函数回归测试。

不联网、不需要 AstrBot 运行时：把 `main.py` 里模块级的纯函数抽出来，用桩替换 astrbot 的导入，
再逐条喂数据比对结果。用途是给"拆包 / 改实现"垫一层安全网 —— 改动前后各跑一次，
绿色才允许继续（见 `TECHNICAL.md` 14.7）。

用法：
    python tests/test_pure.py       只列失败与汇总
    python tests/test_pure.py -v    每一条都列出来
"""

import ast
import importlib
import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"

_MODULES = ("bili.proc", "bili.http", "bili.security", "bili.link", "bili.conf",
            "bili.obs", "bili.download", "bili.play", "bili.visual", "bili.text",
            "bili.card", "main")


# --------------------------------------------------------------------------- 加载


def _stub_astrbot() -> None:
    """占住 astrbot 的模块名，让 main.py 能在没有框架的环境里被 exec。"""

    def mod(name: str, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class AstrMessageEvent:  # 只用于类型注解
        pass

    class Star:
        def __init__(self, *a, **k):
            pass

    class Context:
        pass

    class AstrBotConfig(dict):
        pass

    class Json:
        pass

    class Plain:
        pass

    class Reply:
        def __init__(self, *a, **k):
            self.chain = list(k.get("chain") or [])

    class _Part:
        def __init__(self, *a, **k):
            self.args = a
            self.kw = k

        def mark_as_temp(self):
            self.marked = True
            return self

    class ImageURLPart(_Part):
        class ImageURL:
            def __init__(self, **k):
                self.kw = k

    class TextPart(_Part):
        pass

    class _Decorator:
        """既能当装饰器用，又能继续取属性（filter.EventMessageType.ALL）。"""

        def __call__(self, *a, **k):
            return lambda fn: fn

        def __getattr__(self, _name):
            return self

    class _Filter:
        def __getattr__(self, _name):
            return _Decorator()

    noop = lambda *a, **k: None  # noqa: E731
    log = types.SimpleNamespace(debug=noop, info=noop, warning=noop, error=noop)

    mod("astrbot")
    mod("astrbot.api", logger=log)
    mod("astrbot.api.event", AstrMessageEvent=AstrMessageEvent, filter=_Filter())
    mod("astrbot.api.message_components", Json=Json, Plain=Plain, Reply=Reply)
    mod("astrbot.api.star", Context=Context, Star=Star)
    mod("astrbot.core")
    mod("astrbot.core.agent")
    mod("astrbot.core.agent.message", ImageURLPart=ImageURLPart, TextPart=TextPart)
    mod("astrbot.core.config")
    mod("astrbot.core.config.astrbot_config", AstrBotConfig=AstrBotConfig)
    mod("astrbot.core.utils")
    mod("astrbot.core.utils.astrbot_path", get_astrbot_temp_path=tempfile.gettempdir)


def load_main() -> dict:
    """把插件的各模块导入一遍，返回合并后的命名空间。

    现在的代码是 `main.py` + `bili/` 包的布局：按"插件目录名.模块"的完整路径导入，
    这样相对导入才能成立，与 AstrBot 的加载方式一致（`data.plugins.<目录>.main`）。
    """
    _stub_astrbot()
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    ns: dict = {}
    for name in _MODULES:
        module = importlib.import_module(f"{ROOT.name}.{name}")
        ns.update({k: v for k, v in vars(module).items() if not k.startswith("__")})

    if "bad_host" not in ns:
        raise RuntimeError("导入没有带回函数，检查 sys.path 与目录名")
    return ns


# --------------------------------------------------------------------------- 断言器


class Runner:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.group = ""
        self.total = 0
        self.failed: list[tuple[str, str, object, object]] = []

    def start(self, name: str) -> None:
        self.group = name
        if self.verbose:
            print(f"\n[{name}]")

    def _ok(self, name: str, note: str = "") -> None:
        self.total += 1
        if self.verbose:
            print(f"  ok   {name}{('  ' + note) if note else ''}")

    def _bad(self, name: str, got, want) -> None:
        self.total += 1
        self.failed.append((self.group, name, got, want))

    def eq(self, name: str, got, want) -> None:
        if got == want:
            self._ok(name)
        else:
            self._bad(name, got, want)

    def between(self, name: str, got: float, lo: float, hi: float) -> None:
        if lo <= got <= hi:
            self._ok(name, f"{got:.4g}")
        else:
            self._bad(name, got, f"[{lo:.4g}, {hi:.4g}]")

    def truth(self, name: str, got) -> None:
        self.eq(name, bool(got), True)

    def ok(self, name: str, cond: bool) -> None:
        """断言"条件成立"，比 eq 读起来直白。"""
        if cond:
            self._ok(name)
        else:
            self._bad(name, False, True)

    def report(self) -> int:
        for group, name, got, want in self.failed:
            print(f"FAIL  [{group}] {name}")
            print(f"        得到 {got!r}")
            print(f"        期望 {want!r}")
        bad = len(self.failed)
        tail = "全绿" if not bad else f"{bad} 条失败"
        print(f"\n{self.total} 条断言，{self.total - bad} 通过，{bad} 失败 —— {tail}")
        return 1 if bad else 0


# --------------------------------------------------------------------------- 用例


def case_security(r: Runner, m: dict) -> None:
    bad_host, url_host, host_in, bili_cookie = (
        m["bad_host"], m["url_host"], m["host_in"], m["bili_cookie"],
    )
    marks = m["BILI_MARKS"]

    r.start("安全：拒内网与本机")
    for host in ("localhost", "127.0.0.1", "192.168.1.5", "10.0.0.1", "172.16.0.1",
                 "::1", "fe80::1", "169.254.1.1", "0.0.0.0", "224.0.0.1",
                 "x.local", "x.internal", "intranet", ""):
        r.eq(f"bad_host({host or '空串'}) 应当拒绝", bad_host(host), True)
    for host in ("bilibili.com", "evil.com", "8.8.8.8", "b23.tv"):
        r.eq(f"bad_host({host}) 应当放行", bad_host(host), False)

    r.start("安全：主机名解析")
    r.eq("普通链接", url_host("https://www.bilibili.com/video/BV1xx411c7mD?x=1"), "www.bilibili.com")
    r.eq("大写协议与域名", url_host("HTTP://BILI.COM/A"), "bili.com")
    r.eq("短链", url_host("https://b23.tv/abc"), "b23.tv")
    r.eq("带端口和用户信息", url_host("https://u:p@api.bilibili.com:443/x"), "api.bilibili.com")
    r.eq("结尾的点要剥掉", url_host("https://bilibili.com./x"), "bilibili.com")
    r.eq("非 http 协议", url_host("ftp://x.com/a"), "")
    r.eq("空串", url_host(""), "")
    r.eq("不是网址", url_host("not a url"), "")

    r.start("安全：严格匹配与 Cookie 白名单")
    r.eq("主域名", host_in("https://bilibili.com/x", marks), True)
    r.eq("子域名", host_in("https://api.bilibili.com/x", marks), True)
    r.eq("查询串里带 b23.tv 不算", host_in("https://evil.com/?x=b23.tv", marks), False)
    r.eq("路径里带 bilibili.com 不算", host_in("https://evil.com/bilibili.com/v", marks), False)
    r.eq("b23.tv.evil.com 不算", host_in("https://b23.tv.evil.com/a", marks), False)
    r.eq("notbilibili.com 不算", host_in("https://notbilibili.com", marks), False)
    r.eq("evilbilibili.com 不算", host_in("https://evilbilibili.com", marks), False)
    r.eq("大写域名算", host_in("https://WWW.BILIBILI.COM/x", marks), True)
    r.eq("Cookie 给 B 站", bili_cookie("https://www.bilibili.com/x", "SESSDATA=a"), "SESSDATA=a")
    r.eq("Cookie 给短链", bili_cookie("https://b23.tv/x", "SESSDATA=a"), "SESSDATA=a")
    r.eq("Cookie 给 bili2233", bili_cookie("https://bili2233.cn/x", "SESSDATA=a"), "SESSDATA=a")
    r.eq("Cookie 不给外站", bili_cookie("https://evil.com/?x=b23.tv", "SESSDATA=a"), "")
    r.eq("Cookie 不给 extra 站点", bili_cookie("https://vimeo.com/x", "SESSDATA=a"), "")

    r.start("安全：额外站点白名单")
    r.eq("默认只有 B 站", m["site_marks"](""), marks)
    r.eq("内网地址被丢掉", m["site_marks"]("127.0.0.1"), marks)
    r.eq("正常域名收下", m["site_marks"]("example.com, 127.0.0.1"),
         ("b23.tv", "bili2233.cn", "bilibili.com", "example.com"))
    r.eq("带协议与路径也能认", m["site_marks"]("https://a.com/x, HTTP://B.COM/"),
         ("b23.tv", "bili2233.cn", "bilibili.com", "a.com", "b.com"))


def case_link(r: Runner, m: dict) -> None:
    _first_link, url_key = m["_first_link"], m["url_key"]

    r.start("链接：从文本里取第一条合法链接")
    r.eq("正常的分享文案",
         _first_link("看这个 https://www.bilibili.com/video/BV1xx411c7mD 好看"),
         "https://www.bilibili.com/video/BV1xx411c7mD")
    r.eq("短链", _first_link("https://b23.tv/abc"), "https://b23.tv/abc")
    r.eq("bili2233 短链", _first_link("https://bili2233.cn/abc"), "https://bili2233.cn/abc")
    r.eq("外站的诱饵不算", _first_link("https://evil.com/?x=b23.tv"), "")
    r.eq("跳过外站取后面的", _first_link("https://evil.com https://b23.tv/x"), "https://b23.tv/x")
    r.eq("空文本", _first_link(""), "")

    r.start("链接：归一成同一个视频")
    r.eq("BV 号", url_key("https://www.bilibili.com/video/BV1xx411c7mD?ts=1"),
         "bv:bv1xx411c7md")
    r.eq("短链", url_key("https://b23.tv/AbC123?x=1"), "b23:abc123")
    r.eq("分 P 算不同的东西", url_key("https://www.bilibili.com/video/BV1xx411c7mD?p=3"),
         "bv:bv1xx411c7md:p3")
    r.eq("短链带分 P", url_key("https://b23.tv/AbC123?p=2"), "b23:abc123:p2")

    r.start("链接：分 P 号与 cid 选择")
    page_of, page_cid = m["page_of"], m["page_cid"]
    r.eq("链接里的 p", page_of("https://www.bilibili.com/video/BV1xx411c7mD?p=3"), 3)
    r.eq("没写 p 就是 P1", page_of("https://www.bilibili.com/video/BV1xx411c7mD"), 1)
    view = {"cid": 1, "duration": 10,
            "pages": [{"cid": 7, "duration": 100}, {"cid": 9, "duration": 120}]}
    r.eq("取第 2 个分 P", page_cid(view, 2), (9, 120.0))
    r.eq("超出分 P 数退回顶层", page_cid(view, 9), (1, 10.0))
    r.eq("没有 pages 就用顶层", page_cid({"cid": 5, "duration": 8}, 3), (5, 8.0))
    r.eq("bili2233 同键", url_key("https://bili2233.cn/AbC123"), "b23:abc123")
    r.eq("两种写法同键",
         url_key("https://b23.tv/abc") == url_key("https://b23.tv/abc?share=1"), True)
    r.eq("非 B 站链接原样", url_key("https://evil.com/a"), "https://evil.com/a")


def case_download(r: Runner, m: dict) -> None:
    cdn_hosts, classify_error, cookie_header = (
        m["cdn_hosts"], m["classify_error"], m["cookie_header"],
    )
    retryable = m["retryable"]
    merge_failed = m["merge_failed"]

    r.start("下载：失败节点提取")
    msg = (
        "ERROR: [download] Got error: (<HTTPSConnection(host='upos-sz-estghw.bilivideo.com', "
        "port=443) at 0x1f3bb85cb30>, 'Connection to upos-sz-estghw.bilivideo.com timed out. "
        "(connect timeout=20.0)'). Giving up after 2 retries"
    )
    r.eq("真实报错", cdn_hosts(msg), ["upos-sz-estghw.bilivideo.com"])
    r.eq("两个主机去重排序",
         cdn_hosts("host='b.bilivideo.com' 与 host='a.bilivideo.com' 都超时"),
         ["a.bilivideo.com", "b.bilivideo.com"])
    r.eq("akamai 节点", cdn_hosts("https://upos-hz-mirrorakam.akamaized.net/x"),
         ["upos-hz-mirrorakam.akamaized.net"])
    r.eq("大写归一", cdn_hosts("UpOS-SZ-Estghw.BiliVideo.com"), ["upos-sz-estghw.bilivideo.com"])
    r.eq("没有主机", cdn_hosts("plain text"), [])
    r.eq("空串", cdn_hosts(""), [])
    r.eq("None", cdn_hosts(None), [])

    r.start("下载：错误分类")
    r.eq("404", classify_error("ERROR: 404 Not Found"), "not_found")
    r.eq("视频不存在", classify_error("该视频不存在"), "not_found")
    r.eq("要会员", classify_error("需要大会员才能观看"), "vip")
    r.eq("超时", classify_error("Connection to x timed out"), "network")
    r.eq("连接被拒", classify_error("Connection refused"), "network")
    r.eq("超过大小", classify_error("File is larger than max-filesize"), "too_big")
    r.eq("格式不可用", classify_error("Requested format is not available"), "other")
    r.eq("空串", classify_error(""), "other")
    r.eq(
        "ffmpeg 退出归到可重试",
        classify_error("ERROR: ffmpeg exited with code 4294967158"),
        "network",
    )
    r.ok("ffmpeg 报错值得重试", retryable("ERROR: ffmpeg exited with code 4294967158"))
    r.ok("超时值得重试", retryable("Connection to x timed out"))
    r.ok("格式不可用不该重试", not retryable("Requested format is not available"))
    r.ok("要会员不该重试", not retryable("需要大会员才能观看"))
    r.ok("空串不该重试", not retryable(""))
    r.ok(
        "合并失败认作重试无用",
        merge_failed("ERROR: ffmpeg exited with code 4294967158"),
    )
    r.ok("超时不算合并失败", not merge_failed("Connection to x timed out"))
    r.ok("空串不算合并失败", not merge_failed(""))

    r.start("下载：Cookie 头")
    r.eq("空", cookie_header(""), "")
    r.eq("补 SESSDATA", cookie_header("abc"), "SESSDATA=abc")
    r.eq("两侧空白", cookie_header("  abc  "), "SESSDATA=abc")
    r.eq("已经是完整头", cookie_header("SESSDATA=a; bili_jct=b"), "SESSDATA=a; bili_jct=b")


def case_visual(r: Runner, m: dict) -> None:
    seg, pick_cells, layout = m["segment_by_motion"], m["pick_cells"], m["board_layout"]

    r.start("画面：按变化量分段")
    r.eq("没有分数就没有段", seg([], 100, 9), [])
    flat = [(i * 0.25, 0.0) for i in range(40)]
    got = seg(flat, 10, 4)
    r.eq("平坦画面等分四段", len(got), 4)
    r.eq("从 0 开始", round(got[0][0], 2), 0.0)
    r.eq("收在时长上", round(got[-1][1], 2), 10.0)
    r.eq("边界单调", all(got[i][1] <= got[i + 1][0] for i in range(len(got) - 1)), True)
    steep = [(i * 0.25, 100.0 if i % 8 == 0 else 1.0) for i in range(40)]
    got2 = seg(steep, 10, 4)
    r.eq("剧烈画面也给出四段", len(got2), 4)
    r.eq("每段至少 0.2 秒", all(b - a >= 0.2 - 1e-9 for a, b, _ in got2), True)

    r.start("画面：格数按画面起伏决定")
    r.eq("没有分数时用满", pick_cells([], 9), 9)
    r.eq("只要一格", pick_cells([(0, 1.0)], 1), 1)
    r.eq("平坦降到 6 格", pick_cells([(i, 10.0) for i in range(10)], 9), 6)
    r.eq("中等用 9 格", pick_cells([(i, v) for i, v in enumerate([1] * 8 + [2, 4])], 9), 9)
    r.eq("剧烈用满", pick_cells([(i, v) for i, v in enumerate([1] * 8 + [1, 9])], 9), 9)

    r.start("画面：宫格尺寸")
    cols, rows, w, h, cw, ch = layout(9)
    r.eq("九格是三乘三", (cols, rows), (3, 3))
    r.between("面积贴着预算", w * h, 640_000 * 0.8, 640_000 * 1.25)
    r.eq("每格都够大", cw >= 240 and ch >= 135, True)
    r.eq("fit 在 1..want 之间", 1 <= m["fit_board_cells"](9) <= 9, True)
    r.eq("预算变小时格数不增加", m["fit_board_cells"](9, 16 / 9, 60_000) <= 9, True)

    r.start("画面：token 估算")
    r.eq("1440×900", m["image_tokens"](1440, 900), 807)
    r.eq("更大就更多",
         m["image_tokens"](1440, 900) > m["image_tokens"](720, 450), True)


def case_text(r: Runner, m: dict) -> None:
    r.start("文字：取时间点")

    def in_range(ts, lo, hi):
        return all(lo <= t <= hi for t in ts)

    r.eq("单帧在中间", m["sample_times"](100, 180, 1), [50.0])
    r.eq("六帧都在有效区间", in_range(m["sample_times"](100, 180, 6), 10, 90), True)
    r.eq("时长很短也不越界", in_range(m["sample_times"](3, 180, 6), 0, 3), True)

    r.start("文字：热评拼装")
    hc = m["hot_comments"]
    r.eq("没有热评就是空串", hc([], 5, 200), "")
    r.eq("关掉热评也是空串", hc(["好的"], 0, 200), "")
    r.eq(
        "两条热评带提醒和分隔",
        hc(["说得对", "这个我懂"], 5, 200),
        '评论区（别人说的，不一定对，和画面冲突时以画面为准）：「说得对」「这个我懂」。\n',
    )
    r.eq("超出条数就截断", hc(["一", "二", "三"], 2, 200).count("」"), 2)
    r.eq("空白被压平", hc(["  很多   空白  "], 1, 200).count("很多 空白"), 1)
    r.eq("过长按字数截", len(hc(["字" * 500], 1, 20)) < 120, True)

    r.start("文字：字幕校验")
    prob = m["subtitle_problem"]
    r.eq("覆盖时长对不上", prob("标题", "字" * 300, 100, 150), "覆盖时长对不上")
    r.eq("内容太短", prob("标题", "短", 100, 100), "内容太短")
    r.eq("和标题对不上", prob("测试视频标题", "无关内容" * 50, 100, 100), "和标题对不上")
    r.eq("可用时返回空串", prob("测试视频标题", "测试视频" + "啊" * 200, 100, 100), "")
    r.eq("标题拆成两字词", "测试" in m["title_grams"]("测试视频标题"), True)

    r.start("文字：弱证据判据")
    wp = m["weak_problem"]
    r.eq("标题不符算弱", wp("和标题对不上"), True)
    r.eq("内容太短算弱", wp("内容太短"), True)
    r.eq("串片算硬失败", wp("覆盖时长对不上"), False)
    r.eq("空串不算弱", wp(""), False)

    r.start("文字：按画面时段裁字幕")
    trim = m["trim_heard"]
    r.eq("带时间轴的按时间裁",
         trim("[0:05] 甲\n[0:30] 乙", 10, 100), "[0:05] 甲")
    long_text = "字" * 1000
    out = trim(long_text, 60, 300)
    r.eq("没时间轴的按比例裁", len(out) < 400, True)
    r.eq("裁了要留标记", out.endswith("…（后面没看）"), True)
    r.eq("空串", trim("", 60, 300), "")

    r.start("文字：转写与清洗")
    asr = m["parse_asr"]
    r.eq("sherpa 的 json 行",
         asr('{"text": "你好"}\nnum threads: 4\n0.5: 第二句'), "你好 第二句")
    r.eq("丢掉噪声行", asr("num threads: 4\nReal time factor 0.1"), "")
    r.eq("丢掉路径行", asr("C:\\\\asr\\\\audio.wav\n正常一句"), "正常一句")
    r.eq("清标记与空白", m["clean_text"]("<|zh|>  你好   世界 "), "你好 世界")
    r.eq("相邻重复合并", m["_merge_lines"](["你好", "你好", "世界"]), "你好世界")
    r.eq("分钟秒", m["mmss"](75), "1:15")
    r.eq("负数归零", m["mmss"](-5), "0:00")
    r.eq("超过一小时按分钟数算", m["mmss"](3661), "61:01")
    ok = m["speech_ok"]
    r.eq("纯语气词不算说话", ok("嗯嗯嗯"), False)
    r.eq("同一两个字循环不算说话", ok("对对对对对"), False)
    r.eq("太短不算说话", ok("好的"), False)
    r.eq("正常一句话算说话", ok("这是一段正常的说话内容，有信息量"), True)
    r.eq("英文句子算说话", ok("the quick brown fox jumps"), True)
    r.eq("空串不算说话", ok(""), False)


def case_subtitle_file(r: Runner, m: dict) -> None:
    parse = m["parse_subtitle"]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bili_test_"))

    def write(name: str, body: str) -> str:
        p = tmp / name
        p.write_text(body, encoding="utf-8")
        return str(p)

    r.start("字幕文件：三种格式")
    json3 = {"events": [{"segs": [{"utf8": "你好"}]}, {"segs": [{"utf8": "世界"}]}]}
    r.eq("json3", parse(write("a.json", json.dumps(json3, ensure_ascii=False))), "你好世界")
    srt = "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n2\n00:00:03,000 --> 00:00:04,000\n世界\n"
    r.eq("srt", parse(write("a.srt", srt)), "你好世界")
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好\n"
    r.eq("vtt", parse(write("a.vtt", vtt)), "你好")
    r.eq("空文件", parse(write("e.srt", "  \n")), "")
    r.eq("文件不存在", parse(str(tmp / "nope.srt")), "")


# ------------------------------------------------------------------------- 配置与观测


def case_conf(r: Runner, m: dict) -> None:
    Conf = m["Conf"]
    c = Conf({"entry": {"a": 1}, "visual": {"b": 2, "a": 9}, "text": {}, "safety": {"c": 3}})
    r.eq("entry 组", c.group("entry"), {"a": 1})
    r.eq("缺组给空字典", c.group("nope"), {})
    r.eq("组类型不对也给空", Conf({"entry": 5}).group("entry"), {})
    r.eq("entry 优先", c.get("a"), 1)
    r.eq("别的组的键", c.get("b"), 2)
    r.eq("默认值", c.get("zz", "d"), "d")
    r.eq("拍平后后出现的组覆盖前面的同名键", c.flat()["a"], 9)
    r.eq("拍平后包含各组", sorted(c.flat()), ["a", "b", "c"])
    r.eq("None 配置不炸", Conf(None).get("x", 7), 7)


def case_obs(r: Runner, m: dict) -> None:
    obs = m["obs"]
    t0 = obs.begin()
    r.ok("打点是数字", isinstance(t0, float))
    r.ok("求差非负", obs.span(t0) >= 0.0)
    r.eq("三段格式", obs.segments(25.44, 5.51, 12.44), "下载 25.4s/画面 5.5s/声音 12.4s")
    line = obs.line(418, obs.segments(1.0, 2.0, 3.0), "文字", 0.0)
    r.ok("有字包含字数", "文字 2 字" in line and "约 418 token" in line)
    r.ok("无字写没文字", "没文字" in obs.line(418, "下载 1.0s/画面 2.0s/声音 3.0s", "", 0.0))


def case_play(r: Runner, m: dict) -> None:
    is_pcdn, url_order = m["is_pcdn"], m["url_order"]
    pick_video, pick_audio = m["pick_video"], m["pick_audio"]

    r.start("备用地址：PCDN 识别")
    r.eq("mcdn 算 PCDN", is_pcdn("https://mcdn.bilivideo.cn/x.m4s"), True)
    r.eq("正常节点不算", is_pcdn("https://upos-sz-mirrorzos.bilivideo.com/x.m4s"), False)
    r.eq("空地址不算", is_pcdn(""), False)

    r.start("备用地址：挨个试的顺序")
    node = {
        "baseUrl": "https://mcdn.bilivideo.com/a.m4s",
        "backupUrl": ["https://upos-sz-mirrorzos.bilivideo.com/a.m4s",
                      "https://upos-sz-mirrorzos.bilivideo.com/a.m4s"],
    }
    r.eq("PCDN 排最后、重复去掉", url_order(node),
         ["https://upos-sz-mirrorzos.bilivideo.com/a.m4s",
          "https://mcdn.bilivideo.com/a.m4s"])
    r.eq("没有地址就没有", url_order({}), [])

    r.start("备用地址：挑一路画面")
    videos = [
        {"id": 80, "width": 1920, "height": 1080, "codecs": "hev1.1.6.L150.90"},
        {"id": 32, "width": 852, "height": 480, "codecs": "hev1.1.6.L120.90"},
        {"id": 32, "width": 852, "height": 480, "codecs": "avc1.64001F"},
    ]
    r.eq("480 里挑 avc", pick_video(videos, 480).get("codecs"), "avc1.64001F")
    r.eq("竖屏按短边算", pick_video(
        [{"width": 480, "height": 854, "codecs": "avc1"}, {"width": 720, "height": 1280,
                                                          "codecs": "avc1"}], 480).get("width"),
         480)
    r.eq("都不够小就取最小的", pick_video(
        [{"width": 1280, "height": 720, "codecs": "avc1"},
         {"width": 1920, "height": 1080, "codecs": "avc1"}], 480).get("height"), 720)
    r.eq("没有流就给空", pick_video([], 480), {})

    r.start("备用地址：挑声音")
    r.eq("挑码率最低的", pick_audio(
        [{"bandwidth": 192000}, {"bandwidth": 64000}]).get("bandwidth"), 64000)
    r.eq("没有声音就给空", pick_audio([]), {})


# --------------------------------------------------------------------------- 入口


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    verbose = "-v" in sys.argv[1:] or "--verbose" in sys.argv[1:]
    if not MAIN.is_file():
        print(f"找不到 {MAIN}")
        return 2
    mod = load_main()
    r = Runner(verbose)
    for fn in (case_security, case_link, case_conf, case_obs, case_download, case_play,
               case_visual, case_text, case_subtitle_file):
        fn(r, mod)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())

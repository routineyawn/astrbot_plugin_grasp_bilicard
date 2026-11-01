"""观测：一次处理的计时，以及那行日志的固定写法。

为什么要单独一层：之前用 `time.time()` 打点、用 `time.perf_counter()` 求差，
混着用会算出负数（-1793341009.1s）；而且下载慢和画面慢从日志上看不出区别。
现在两条规矩：

1. 求差用同一只钟——`begin()` 打点，`span()` 求差，都是 perf_counter；
2. 一段一量，画面和声音是并行跑的（各自从自己的起点算），不串成链。

段名和格式只在这里写一次，以后加一段（比如"清理"）也在这里加。
"""

import time


def begin() -> float:
    """打点，返回起点（单调时钟，不受系统时间调整影响）。"""
    return time.perf_counter()


def span(t0: float) -> float:
    """从打点到现在的秒数。"""
    return time.perf_counter() - t0


def segments(download: float, visual: float, sound: float) -> str:
    """三段耗时拼成一句：下载 25.4s/画面 5.5s/声音 12.4s。"""
    return f"下载 {download:.1f}s/画面 {visual:.1f}s/声音 {sound:.1f}s"


def line(tokens: int, segs: str, heard: str, started: float) -> str:
    """整条日志的后半句：约 N token，<三段>，文字 M 字，用时 T 秒。"""
    chars = f"文字 {len(heard)} 字" if heard else "没文字"
    return f"约 {tokens} token，{segs}，{chars}，用时 {time.time() - started:.1f} 秒"

"""Grasp-BiliCard —— 让对话模型真正"看过"QQ 里的 B 站分享卡片。

流程：
1. 收到卡片（QQ 小程序的 json 消息段）时先记下链接，并把卡片换成一行提示文字，
   这样即使卡片没配文字，对话流程也会启动；
2. 在发给模型之前（on_llm_request）下载视频、截几张画面（可选把声音转成文字），
   把说明和画面放进这一轮上下文。

所有下载、截图、本机转写都在本机完成；失败只记日志，不影响正常回话。
"""

import asyncio
import os
import shutil
import time
import uuid
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .bili import obs
from .bili.card import CardMixin
from .bili.conf import Conf
from .bili.proc import locate_ffmpeg
from .bili.link import chain_link, share_link, share_title, url_key


class BiliCard(CardMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.c = Conf(config)
        self.tmp = os.path.join(get_astrbot_temp_path(), "bili_card")
        self._no_ffmpeg_warned = False
        # 群里发卡片通常跟一句 @ 消息，卡片本身不 @ 时先把链接记下来，等下一句用。
        # 记的时候带上发送者：群里别人发卡片、你去 @，不该用他发的那张。
        self._recent: dict[str, tuple[str, str, float, str]] = {}
        # 每人每天最多处理几条；0 表示不限。计数放在内存里，重启归零。
        self._count: dict[str, tuple[int, str]] = {}
        # 同一会话同一链接的处理结果缓存：GCP 会把一轮对话拆成多次 LLM 请求，
        # 不缓存的话同一条视频会被重复下载、重复抽帧三次以上
        self._done: dict[str, tuple[str, str, list[str], float]] = {}
        # 卡片到达就先把视频下好：键是 url_key，值是 (路径, 真实时长, 开始时间)
        self._prefetch: dict[str, tuple[str, float, float]] = {}
        self._prefetching: dict[str, asyncio.Task] = {}
        # 预取把整条链路都跑完，结果（说明文字 + 画面路径）存在这里，@ 时直接注入
        self._prebuilt: dict[str, dict] = {}
        # 同一条链接正在处理的标记：第二轮等第一轮，别各跑一遍
        self._busy: dict[str, asyncio.Event] = {}
        # 同时最多两个下载：连发几张卡片时不至于把带宽和磁盘一起占满
        self._dl_slot = asyncio.Semaphore(2)
        self._sweep()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=95)
    async def mark_card(self, event: AstrMessageEvent):
        if not self._get("watch_qq_card", True):
            return
        msg = getattr(event, "message_obj", None)
        chain = (msg.message if msg else []) or []
        marks = self._marks()
        card = next((c for c in chain if share_link(c, marks)), None)
        if not card:
            return
        url = share_link(card, marks)
        title = share_title(card)
        self._recent[event.session_id] = (
            url,
            title,
            time.time(),
            str(event.get_sender_id() or ""),
        )
        # 先把视频下起来，等下一句 @ 时就不用再等下载
        self._prefetch_begin(url)
        quiet = (
            not event.is_private_chat()
            and self._get("require_at_in_group", True)
            and not event.is_at_or_wake_command
        )
        # 不管这一轮看不看，都把卡片换成一句文字。
        # 不换的话这条消息对别的插件就是空的（决策、记忆都会拿到空文本）。
        hint = f"[对方分享了一个视频《{title}》]" if title else "[对方分享了一个视频]"
        chain[chain.index(card)] = Plain(hint)
        text = "".join(c.text for c in chain if isinstance(c, Plain)).strip()
        msg.message_str = text
        event.message_str = text
        if quiet:
            logger.info(f"[bili_card] 记下群里的卡片，等下一句 @ 时用：{title or '（没标题）'}")
            return
        event._bili_link = url
        event._bili_title = title
        logger.info(f"[bili_card] 收到卡片：{title or '（没标题）'}")

    @filter.on_llm_request()
    async def attach_frames(self, event: AstrMessageEvent, req):
        if not self._get("watch_qq_card", True):
            return
        url = chain_link(event, self._recent, self._marks(), self._wait())
        if not url:
            return
        if not event.is_private_chat() and self._get("require_at_in_group", True) and not event.is_at_or_wake_command:
            return
        ffmpeg = locate_ffmpeg(self._get("ffmpeg_bin") or "")
        if not ffmpeg:
            if not self._no_ffmpeg_warned:
                self._no_ffmpeg_warned = True
                logger.warning("[bili_card] 找不到 ffmpeg，看不了视频")
            return
        # 命中缓存的这一轮不再下载，也不占次数
        hit = self._done.get(getattr(event, "session_id", "") or "")
        fresh = not (hit and url_key(hit[0]) == url_key(url) and time.time() - hit[3] < 600)
        if fresh and not self._allow(str(event.get_sender_id() or "")):
            limit = int(self._get("daily_limit", 0) or 0)
            logger.info(f"[bili_card] 这个人今天已经看了 {limit} 条，到上限了，这次跳过")
            self._say(req, f"[对方今天发来的视频已经到上限（{limit} 条），这次没看。别装看过。]")
            return
        self._sweep()
        work = os.path.join(self.tmp, uuid.uuid4().hex)
        os.makedirs(work, exist_ok=True)
        t0 = time.time()
        t0p = obs.begin()
        try:
            await self._run(event, req, url, ffmpeg, work, t0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_card] 处理卡片失败：{e}")
            # 失败时没有图片要留给模型，直接清掉这次的工作目录
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _say(req, text: str):
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        # 说明文字只在这一轮有效，否则会永久留在会话历史里
        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())

"""入口与调度：收到消息后怎么排队、什么时候下载、什么时候放弃、怎么塞进上下文。

类本身写在 `main.py` 里（AstrBot 按定义所在模块登记插件），这里只放方法。
"""

import asyncio
import os
import shutil
import time
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.agent.message import ImageURLPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from . import obs
from .conf import Conf
from .download import download, media_info
from .proc import locate_ffmpeg, mmss
from .link import url_key
from .security import site_marks
from .text import (
    SUBTITLE_WHY,
    fetch_comments,
    fetch_desc,
    fetch_meta,
    fetch_subtitle,
    find_asr,
    hot_comments,
    speech_ok,
    transcribe_api,
    transcribe_local,
    trim_heard,
)
from .visual import grab_audio, make_visual, storyboard_visual


# 默认引导语。配置了 prompt_template 就整段替换，$title/$length/$cut/$shot/$sound 会被填上。
DEFAULT_PROMPT = (
    "[对方分享了视频$title，时长 $length$cut。"
    "$meta"
    "$desc"
    "$shot$sound"
    "$comments"
    "画面按时间抽样、不连续：请连起来说清视频讲了什么、有哪几件事，"
    "用你自己的话，别逐格描述，也别提截图、宫格、抽帧、转写。"
    "画面只给你看这一次，没看清就说没看清，别编。]"
)


class CardMixin:
    def _get(self, key: str, default=None):
        """配置分四组放，统一交给 bili.conf 取一项。"""
        return self.c.get(key, default)

    def _marks(self) -> tuple[str, ...]:
        return site_marks(self._get("extra_sites") or "")

    def _allow(self, sender: str) -> bool:
        """按发送者算每天几次；0 表示不限。"""
        try:
            limit = int(self._get("daily_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True
        day = time.strftime("%Y-%m-%d")
        count, saved = self._count.get(sender, (0, day))
        if saved != day:
            count = 0
        if count >= limit:
            self._count[sender] = (count, day)
            return False
        self._count[sender] = (count + 1, day)
        return True

    def _wait(self) -> float:
        """群里先发卡片、后 @ 时，这张卡片最多等多少秒。"""
        try:
            return float(max(30, int(self._get("card_wait_seconds", 300) or 300)))
        except (TypeError, ValueError):
            return 300.0

    def _compose_text(
        self,
        title: str,
        length: str,
        cut: str,
        shot: str,
        sound: str,
        desc: str = "",
        meta: str = "",
        comments: str = "",
    ) -> str:
        """拼这一轮的说明文字；配置了 prompt_template 就用用户自己的模板。"""
        tpl = (self._get("prompt_template") or "").strip() or DEFAULT_PROMPT
        for key, val in (
            ("$title", title),
            ("$length", length),
            ("$cut", cut),
            ("$shot", shot),
            ("$sound", sound),
            ("$desc", desc),
            ("$meta", meta),
            ("$comments", comments),
        ):
            tpl = tpl.replace(key, val)
        return tpl

    def _sweep(self):
        now = time.time()
        wait = self._wait()
        for key, item in list(self._recent.items()):
            if now - item[2] > wait:
                self._recent.pop(key, None)
        for key, item in list(self._done.items()):
            if now - item[3] > 600:
                self._done.pop(key, None)
        for key, item in list(getattr(self, "_prebuilt", {}).items()):
            if now - item.get("ts", 0) > 600:
                self._prebuilt.pop(key, None)
        for key, item in list(self._prefetch.items()):
            if now - item[2] > 600 or not os.path.isfile(item[0]):
                self._prefetch.pop(key, None)
        try:
            if not os.path.isdir(self.tmp):
                return
            keep = max(1, int(self._get("sweep_minutes", 20) or 20)) * 60
            for name in os.listdir(self.tmp):
                p = os.path.join(self.tmp, name)
                if os.path.isdir(p) and now - os.path.getmtime(p) > keep:
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


    async def _fetch(self, url, work, ffmpeg, max_mb, height, max_seconds):
        """所有下载都走这里：同时最多两个，避免连发卡片时抢带宽。"""
        slot = getattr(self, "_dl_slot", None)
        if slot is not None:
            await slot.acquire()
        try:
            return await download(
                url,
                work,
                ffmpeg,
                max_mb,
                self._get("bili_cookie") or "",
                height,
                max_seconds,
            )
        finally:
            if slot is not None:
                slot.release()

    def _waited(self, url: str) -> float:
        """卡片到达到现在隔了多久。

        防抖插件（私聊）会先把消息压住再交上来，这段时间不该算进处理耗时；
        单独记一笔，方便对账「你等了几秒」和「我处理了几秒」。
        """
        key = url_key(url)
        for item in self._recent.values():
            if item[0] and url_key(item[0]) == key:
                return max(0.0, time.time() - item[2])
        return 0.0

    def _prefetch_begin(self, url: str) -> None:
        """卡片一到就开始下载，不等下一句 @。

        群里从「有人发卡片」到「你 @ 它」通常隔几十秒，这段空档正好用来下载；
        等真要回答时只剩下解码和转写。占一次并发达不到，失败也不影响回答。
        """
        if not self._get("prefetch_on_card", True):
            return
        key = url_key(url)
        if not key or key in self._prefetch or key in self._prefetching:
            return
        ffmpeg = locate_ffmpeg(self._get("ffmpeg_bin") or "")
        if not ffmpeg:
            return
        # 记下任务本身：@ 来得早时直接等它，不要另起一份下载
        self._prefetching[key] = asyncio.create_task(self._prefetch_job(key, url, ffmpeg))

    async def _prefetch_job(self, key: str, url: str, ffmpeg: str) -> None:
        """卡片到达就把整条链路跑完：下载 + 画面 + 文字。

        跑完的结果留在内存里，等 @ 的那一刻直接注入，几乎没有等待；
        下载、截帧都只在这一次发生，@ 之后不再重复。
        """
        work = os.path.join(self.tmp, "pre_" + key.replace(":", "_").replace("/", "_"))
        try:
            os.makedirs(work, exist_ok=True)
            max_seconds = max(5, int(self._get("clip_seconds", 300) or 300))
            max_mb = max(10, int(self._get("size_limit_mb", 120) or 120))
            style = (self._get("frame_style") or "board").strip().lower()
            height = 720 if style == "frames" else 480
            t0 = obs.begin()
            path, reason, real_dur = await self._fetch(
                url, work, ffmpeg, max_mb, height, max_seconds
            )
            # 预取这一段自己计时：交付时三段耗时不能全是 0，否则看不出成本
            dl_s = obs.span(t0) if path else 0.0
            if not path:
                logger.info(f"[bili_card] 预取没下到视频（{reason}），等 @ 时再兜底")
                shutil.rmtree(work, ignore_errors=True)
                return
            duration, has_audio = await media_info(ffmpeg, path)
            if real_dur > 0:
                duration = real_dur
            sub_task, desc_task, extra_task = self._side_tasks(url, work)
            built = await self._build(
                ffmpeg,
                url,
                "",
                path,
                duration,
                has_audio,
                max_seconds,
                self.c.flat(),
                work,
                sub_task,
                desc_task,
                extra_task,
            )
            if built.get("empty"):
                logger.info("[bili_card] 预取没截出画面，等 @ 时再试")
                return
            self._prebuilt[key] = built
            built["dl_s"] = dl_s
            logger.info(
                f"[bili_card] 卡片到了先做好画面和文字：{obs.span(t0):.1f} 秒，等 @ 时直接用"
            )
        except Exception as e:  # noqa: BLE001
            logger.info(f"[bili_card] 预取没成功，不影响回答：{e}")
        finally:
            self._prefetching.pop(key, None)

    async def _extra(self, url: str) -> tuple[str, list[str]]:
        """分区/标签 + 热评：都只是锦上添花，失败就当没有。"""
        meta = ""
        aid = 0
        if self._get("include_tags", True):
            try:
                meta, aid = await fetch_meta(url, self._get("bili_cookie") or "")
            except Exception as e:  # noqa: BLE001
                logger.info(f"[bili_card] 分区标签没拿到，跳过：{e}")
        want = 0
        if self._get("comments_max", 5) or self._get("comments_max_no_subtitle", 8):
            want = 20
        comments: list[str] = []
        if want:
            try:
                comments = await fetch_comments(
                    self._get("bili_cookie") or "", aid, want
                )
            except Exception as e:  # noqa: BLE001
                logger.info(f"[bili_card] 热评没拿到，跳过：{e}")
        return meta, comments

    def _side_tasks(self, url: str, work: str) -> tuple:
        """字幕、简介、分区热评都不依赖视频文件，和下载同时开跑。"""
        return (
            asyncio.create_task(self._subtitle(url, work)),
            asyncio.create_task(self._desc(url)),
            asyncio.create_task(self._extra(url)),
        )

    async def _subtitle(self, url: str, work: str) -> tuple[str, str]:
        """只要 bvid 就能取字幕，不依赖视频文件，所以能和下载同时跑。"""
        if not self._get("prefer_subtitles", True):
            return "", "off"
        return await fetch_subtitle(url, work, self._get("bili_cookie") or "")

    async def _desc(self, url: str) -> str:
        if not self._get("include_desc", True):
            return ""
        return await fetch_desc(url, self._get("bili_cookie") or "")

    async def _build(
        self,
        ffmpeg: str,
        url: str,
        label: str,
        path: str,
        duration: float,
        has_audio: bool,
        max_seconds: int,
        flat: dict,
        work: str,
        sub_task,
        desc_task,
        extra_task,
    ) -> dict:
        """从已下载的文件做出「说明文字 + 宫格图」，不碰上下文。

        预取和正常请求共用这一段：谁先跑完谁的结果进缓存，另一路直接复用。
        """
        av0 = obs.begin()
        visual, (heard, heard_why), desc, (meta, comments) = await asyncio.gather(
            make_visual(ffmpeg, path, duration, max_seconds, flat, work),
            sub_task,
            desc_task,
            extra_task,
        )
        av_s = obs.span(av0)
        if not visual["images"]:
            try:
                os.remove(path)
            except OSError:
                pass
            return {"empty": True}

        if heard:
            before = len(heard)
            heard = trim_heard(heard, max_seconds, duration)
            cut_note = (
                f"，按前 {max_seconds // 60} 分钟裁到 {len(heard)} 字"
                if len(heard) < before
                else ""
            )
            logger.info(f"[bili_card] 用了视频自带的字幕：{len(heard)} 字{cut_note}")

        mode = (self._get("asr_mode") or "off").strip().lower()
        asr_why = ""
        heard_src = "字幕" if heard else ""
        asr0 = obs.begin()
        wavs = (
            await grab_audio(ffmpeg, path, max_seconds, os.path.join(work, "audio"))
            if has_audio and mode != "off" and not heard
            else []
        )
        if wavs:
            try:
                if mode == "local":
                    exe, model, tokens = find_asr(self._get("local_asr_home") or "")
                    if not exe:
                        asr_why = "没找到本机转写程序"
                        logger.info("[bili_card] 没找到本机 ASR 程序，这次只听画面")
                    else:
                        heard = await transcribe_local(exe, model, tokens, wavs)
                        if not heard:
                            asr_why = "转写没认出字"
                        heard_src = "自动转写，可能有错字"
                elif mode == "openai":
                    key = (self._get("remote_asr_key") or "").strip()
                    if not key:
                        asr_why = "没填远程转写 key"
                        logger.info("[bili_card] 没填远程转写的 key，这次只听画面")
                    else:
                        heard = await transcribe_api(
                            wavs,
                            self._get("remote_asr_base") or "https://api.siliconflow.cn/v1",
                            key,
                            (self._get("remote_asr_model") or "FunAudioLLM/SenseVoiceSmall").strip(),
                        )
                        if not heard:
                            asr_why = "转写没认出字"
                        heard_src = "自动转写，可能有错字"
            except Exception as e:  # noqa: BLE001
                asr_why = "转写出错"
                logger.warning(f"[bili_card] 声音转文字失败，只看画面：{e}")

        asr_s = obs.span(asr0)
        # 纯音乐/环境音时转写也会吐几个字（"嗯嗯嗯"），当没听到处理，别浪费上下文
        if wavs and heard and not speech_ok(heard):
            logger.info(
                f"[bili_card] 转写只出来 {len(heard)} 个字（{heard[:20]}），当成没听到内容"
            )
            heard = ""
            asr_why = "转写只出来几个字，可能只是音乐或环境音"
        try:
            os.remove(path)
        except OSError:
            pass
        # 音频已经变成文字，留着没用；画面要留到模型真的读图那一刻，由 _sweep 20 分钟后清
        shutil.rmtree(os.path.join(work, "audio"), ignore_errors=True)

        length = mmss(duration) if duration else "未知"
        cut = f"，只看了前 {max_seconds // 60} 分钟" if duration > max_seconds else ""
        if heard:
            limit = max(0, int(self._get("subtitle_limit", 800) or 0))
            talk = heard[:limit] if limit else heard
            tail = "…（太长截断）" if limit and len(heard) > limit else ""
            sound = f"视频里说的话（{heard_src}）：「{talk}{tail}」。"
        elif has_audio:
            reasons = []
            if heard_why and heard_why != "ok":
                reasons.append(SUBTITLE_WHY.get(heard_why, f"字幕没拿到（{heard_why}）"))
            if asr_why:
                reasons.append(asr_why)
            elif mode == "off":
                reasons.append("转写是关的")
            sound = f"视频有声音，但这次没听到内容（{'；'.join(reasons) or '没有可用字幕'}）。"
        else:
            sound = ""
        desc_line = f"UP 主写的内容说明：{desc}\n" if desc else ""
        meta_line = f"{meta}。\n" if meta else ""
        # 没字幕的时候画面最贫瘠，热评这时候最有用，多给几条
        comment_cap = (
            self._get("comments_max", 5)
            if heard
            else self._get("comments_max_no_subtitle", 8)
        )
        comment_line = hot_comments(
            comments,
            int(comment_cap or 0),
            int(self._get("comments_chars", 200) or 200),
        )
        comment_n = comment_line.count("」「") + 1 if comment_line else 0
        shown = len(talk) if heard else 0
        heard_part = (
            f"字幕 {len(heard)} 字（注入 {shown} 字）"
            if heard and shown != len(heard)
            else f"字幕 {len(heard)} 字"
        )
        logger.info(
            "[bili_card] 文字构成："
            f"{heard_part}，简介 {len(desc)} 字，"
            f"{meta or '标签 无'}，"
            f"热评 {comment_n} 条 {len(comment_line)} 字"
        )
        text = self._compose_text(
            label, length, cut, visual["hint"], sound, desc_line, meta_line, comment_line
        )
        return {
            "text": text,
            "images": list(visual["images"]),
            "frames": visual["frames"],
            "board": visual["board"],
            "tokens": visual["tokens"],
            "heard": heard,
            "av_s": av_s,
            "asr_s": asr_s,
            "ts": time.time(),
        }

    def _deliver(self, req, built: dict) -> None:
        self._say(req, built["text"])
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        for img in built["images"]:
            req.extra_user_content_parts.append(
                ImageURLPart(image_url=ImageURLPart.ImageURL(url=img)).mark_as_temp()
            )

    async def _run(self, event, req, url: str, ffmpeg: str, work: str, t0: float):
        """同一链接的并发入口：先看缓存，再看有没有同一轮在跑，最后才自己干。"""
        key = url_key(url)
        hit = self._done.get(key) if key else None
        if hit and time.time() - hit[3] < 600:
            imgs = [p for p in hit[2] if os.path.isfile(p)]
            if imgs:
                self._deliver(req, {"text": hit[1], "images": imgs})
                logger.info("[bili_card] 复用 10 分钟内的处理结果，这次不重新下载")
                return
        waited = self._busy.get(key) if key else None
        if waited is not None:
            logger.info("[bili_card] 同一条还在处理，等它做完直接复用")
            try:
                await asyncio.wait_for(waited.wait(), 120)
            except asyncio.TimeoutError:
                logger.info("[bili_card] 等太久了，自己再跑一遍")
            hit = self._done.get(key) if key else None
            if hit and time.time() - hit[3] < 600:
                imgs = [p for p in hit[2] if os.path.isfile(p)]
                if imgs:
                    self._deliver(req, {"text": hit[1], "images": imgs})
                    logger.info("[bili_card] 复用刚做完的结果，这次没重复处理")
                    return
        mine = asyncio.Event()
        if key:
            self._busy[key] = mine
        try:
            await self._run_body(event, req, url, ffmpeg, work, t0)
        finally:
            if key and self._busy.get(key) is mine:
                self._busy.pop(key, None)
            mine.set()

    async def _run_body(self, event, req, url: str, ffmpeg: str, work: str, t0: float):
        flat = self.c.flat()
        title = getattr(event, "_bili_title", "") or ""
        label = f"《{title}》" if title else ""
        key = url_key(url)
        # 同一链接 10 分钟内直接复用：键是链接而不是会话，
        # 同一个视频在几个群里转来转去也只用做一次
        hit = self._done.get(key) if key else None
        if hit and time.time() - hit[3] < 600:
            imgs = [p for p in hit[2] if os.path.isfile(p)]
            if imgs:
                self._deliver(req, {"text": hit[1], "images": imgs})
                logger.info("[bili_card] 复用 10 分钟内的处理结果，这次不重新下载")
                return
        waited = self._waited(url)
        if waited >= 1.0:
            logger.info(
                f"[bili_card] 卡片到开始处理隔了 {waited:.1f} 秒（防抖/等待，不算在下面的用时里）"
            )
        max_seconds = max(5, int(self._get("clip_seconds", 300) or 300))
        max_mb = max(10, int(self._get("size_limit_mb", 120) or 120))
        style = (self._get("frame_style") or "board").strip().lower()
        height = 720 if style == "frames" else 480

        # 卡片到达时整条链路已经跑完了（见 _prefetch_job），这里直接交付
        pre = self._prebuilt.pop(key, None) if key else None
        if pre and all(os.path.isfile(p) for p in pre["images"]):
            self._deliver(req, pre)
            if key:
                self._done[key] = (url, pre["text"], list(pre["images"]), pre["ts"])
            logger.info("[bili_card] 用卡片到达时预取的结果，这次不用再下载或抽帧")
            logger.info(
                f"{self._frame_label(pre['frames'], pre['board'])}，"
                + obs.line(
                    pre["tokens"],
                    obs.segments(pre.get("dl_s", 0.0), pre["av_s"], pre["asr_s"]),
                    pre["heard"],
                    t0,
                )
            )
            return

        # 预取还在跑（@ 来早了）：等它，别另起一份下载和解码
        running = self._prefetching.get(key) if key else None
        if running is not None and not running.done():
            logger.info("[bili_card] 预取还在跑，等它做完")
            try:
                await asyncio.wait_for(asyncio.shield(running), 90)
            except Exception:  # noqa: BLE001
                pass
            pre = self._prebuilt.pop(key, None)
            if pre and all(os.path.isfile(p) for p in pre["images"]):
                self._deliver(req, pre)
                if key:
                    self._done[key] = (
                        url,
                        pre["text"],
                        list(pre["images"]),
                        pre["ts"],
                    )
                logger.info("[bili_card] 预取刚好跑完，直接用它的结果")
                logger.info(
                    f"{self._frame_label(pre['frames'], pre['board'])}，"
                    + obs.line(
                        pre["tokens"],
                        obs.segments(pre.get("dl_s", 0.0), pre["av_s"], pre["asr_s"]),
                        pre["heard"],
                        t0,
                    )
                )
                return

        # 字幕、简介、分区热评只要 bvid，不依赖视频文件，和下载同时开始
        sub_task, desc_task, extra_task = self._side_tasks(url, work)
        pre_file = self._prefetch.pop(key, None) if key else None
        if pre_file and os.path.isfile(pre_file[0]) and time.time() - pre_file[2] < 600:
            path, reason, real_dur = pre_file[0], "", pre_file[1]
            dl_s = 0.0
            logger.info("[bili_card] 用卡片到达时预下的视频，省掉下载这段")
        else:
            dl0 = obs.begin()
            path, reason, real_dur = await self._fetch(
                url, work, ffmpeg, max_mb, height, max_seconds
            )
            dl_s = obs.span(dl0) if path else 0.0  # 失败走故事板时没有下载段

        if not path:
            for task in (sub_task, desc_task, extra_task):
                task.cancel()
            why = {
                "not_found": "视频不存在或已被删除",
                "vip": "这个视频要登录或大会员才看得到",
                "too_big": "视频超过了大小上限",
                "network": "网络或 B 站那边超时了",
                "no_ytdlp": "服务器上没装下载组件",
            }.get(reason, "下载没成功")
            # 还有一路兜底：B 站官方故事板，不下载视频也能拿到几张缩略图
            sb = await storyboard_visual(ffmpeg, url, work, flat)
            if sb["images"]:
                text = self._compose_text(
                    label,
                    "",
                    "",
                    sb["hint"],
                    f"视频没下载下来（{why}），这次也没听到声音。",
                )
                self._deliver(req, {"text": text, "images": list(sb["images"])})
                if key:
                    self._done[key] = (url, text, list(sb["images"]), time.time())
                logger.info(
                    f"[bili_card] 下载失败（{reason}），改用官方故事板："
                    f"{sb['frames']} 格，约 {sb['tokens']} token，用时 {time.time() - t0:.1f} 秒"
                )
                return
            self._say(req, f"[对方分享了一个视频{label}，但{why}，你只看到标题，别编视频里的内容。]")
            logger.info(f"[bili_card] 下载失败（{reason}），只给标题")
            return

        duration, has_audio = await media_info(ffmpeg, path)
        # 下载只取了前 max_seconds 秒，ffprobe 量到的是片段长度；
        # 字幕校验、时长显示、裁剪提示都要按真实时长来
        if real_dur > 0:
            duration = real_dur

        built = await self._build(
            ffmpeg,
            url,
            label,
            path,
            duration,
            has_audio,
            max_seconds,
            flat,
            work,
            sub_task,
            desc_task,
            extra_task,
        )
        if built.get("empty"):
            self._say(req, f"[对方分享了一个视频{label}，但画面没截出来，你看不到内容。别编视频里的东西。]")
            logger.info("[bili_card] 没截出画面")
            return
        self._deliver(req, built)
        if key:
            self._done[key] = (url, built["text"], list(built["images"]), built["ts"])
        logger.info(
            f"{self._frame_label(built['frames'], built['board'])}，"
            + obs.line(
                built["tokens"],
                obs.segments(dl_s, built["av_s"], built["asr_s"]),
                built["heard"],
                t0,
            )
        )

    @staticmethod
    def _frame_label(frames: int, board: bool) -> str:
        return "1 张 " + str(frames) + " 格宫格" if board else str(frames) + " 张画面"

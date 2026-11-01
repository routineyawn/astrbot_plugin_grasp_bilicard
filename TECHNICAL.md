# 实现文档：Grasp-BiliCard

版本：v0.1.0 ｜ 目标平台：aiocqhttp（NapCat / onebot v11）｜ 许可：MIT

---

## 1. 它解决的问题

QQ 里转发 B 站视频，收到的是一条 **QQ 小程序卡片**，在 onebot 协议里是 `message` 数组中的
`{"type": "json", "data": {"data": "<JSON 字符串>"}}`。AstrBot 会把它适配成 `Json` 组件，
但 `message_str` 里不会出现任何文字——模型只看到"有一个消息段"，既不知道是视频，更不知道内容。

本插件的作用就是把这层卡片壳子拆开，把里面能拿到的东西（链接、标题、画面、语音转写）
变成模型能直接读的输入。

## 2. 真实数据结构

一条真实的 QQ 里 B 站卡片（2026-10-31 抓自 NapCat 实时调试）：

```json
{
  "type": "json",
  "data": {
    "data": "{\"app\":\"com.tencent.miniapp_01\",\"meta\":{\"detail_1\":{\"qqdocurl\":\"https://b23.tv/RBUgzwL?share_medium=android&share_source=qq&bbid=...\",\"desc\":\"（视频标题，示例已替换）\",\"title\":\"哔哩哔哩\"}},\"prompt\":\"[QQ小程序]...\",\"url\":\"m.q.qq.com/a/s/f0ac35b7f22f7587a5406eab22fae8b7\"}}"
  }
}
```

关键点：

- 外层 `data.data` 是一段**字符串**，要再 `json.loads` 一次；
- 真正的链接在 `meta.detail_1.qqdocurl`（有时是 `jumpUrl`）；
- `meta.detail_1.desc` 是视频标题；
- `url` 字段是 `m.q.qq.com/...` 的中间页，**不能**直接给 yt-dlp，只能当兜底线索；
- 短链 `b23.tv/xxx` 会 302 到 `bilibili.com/video/BVxxx`，yt-dlp 能直接吃。

插件里由 `share_link()` / `share_title()` 做这层拆解，任何用户自定义卡片格式的适配都只改这两个函数。

## 3. 整体流程

```
QQ 消息
  │
  ├─(钩子1) mark_card  ── 找 Json 段 → 取 b23.tv 链接 → 记进内存（按会话，10 分钟）
  │                        ├─ 私聊 / 群里 @ 了：把卡片段换成"[对方分享了一个 B 站视频《标题》]"
  │                        └─ 群里没 @：只记下，不打断消息
  │
  ▼
AstrBot 决定要调用模型
  │
  ├─(钩子2) attach_frames ── 找链接（当前链 → 引用链 → 刚记下的卡片）
  │      │
  │      ├─ locate_ffmpeg()      找 ffmpeg，找不到就放弃
  │      ├─ download()           yt-dlp 下 mp4（宫格 480P、逐帧 720P，可带 Cookie），大小上限 size_limit_mb
  │      ├─ media_info()         ffprobe 拿时长、有没有音轨
  │      ├─ make_visual() ──┐    画面这一段的总入口（与字幕同时起，见下）
  │      │   ├─ scan_motion()        （frame_style=board）4fps 扫 64×36 画面变化，HSV 差分
  │      │   ├─ pick_cells()         按起伏把格数在 6~want 之间自适应
  │      │   ├─ segment_by_motion()  按累计变化量把前 N 秒等分成 N 段
  │      │   ├─ pick_sharp_frame()   每段取 3 个候选点，按清晰度留一张
  │      │   ├─ extract_frames()     ffmpeg 每点截一张 1280 宽 jpg
  │      │   ├─ compose_board()      （board）Pillow 按 640k 面积预算拼一张宫格，烧编号
  │      │   └─ sample_times()/extract_frames()（frames）在 10%~90% 均匀取 frame_count 个点
  │      └─ fetch_subtitle()     （prefer_subtitles 时）同时去拉视频自带字幕，拿到就跳过转写
  │      ├─ grab_audio()         （asr_mode != off 且没字幕时）切 30 秒一段 16k wav
  │      ├─ transcribe_local() / transcribe_api()  转成文字
  │      ├─ 删掉视频和音频，只留画面
  │      └─ 注入 TextPart（说明+时长+时间点+转写）与 ImageURLPart（每帧）
  │
  ▼
对话模型：同时看到画面和声音文字，按人设回话
```

> 动手改代码前先读第 14 节：那里写死了改动必须遵守的边界。

## 4. 钩子细节

### 4.1 `mark_card`（`@filter.event_message_type(ALL, priority=95)`）

- 扫当前消息链，找第一个 `share_link()` 命中的 `Json` 组件；
- 找到后**无论有没有 @ 都先记进 `self._recent[session_id] = (url, title, ts)`**，
  这是群聊能用的关键：群里的卡片通常是一条独立消息，@ 是下一条；
- 私聊或群里 @ 了，再把卡片段替换成一行纯文本，并重组 `message_str`，
  否则模型连"有人分享了视频"都不知道；
- `require_at_in_group` 只决定**要不要改消息**，不决定**要不要记下卡片**。

### 4.2 `attach_frames`（`@filter.on_llm_request()`）

- 只在"这一轮真的要发模型"时触发，所以下载成本只花在会看到的轮次上；
- 取链接顺序：`event._bili_link` → 当前链 → `Reply.chain` → `_recent`（10 分钟内）；
- 群聊里同样遵守 `require_at_in_group`；
- 失败时注入一句"没下载下来/没截出来，别编"，保证模型不瞎猜。

### 4.3 说明文字的出发点

注入的说明里固定带这几句，每一句都对着一种具体的翻车：

- 「这些画面是按时间抽的样，不是连续播放」——不说这句，模型会把几格当成连续镜头，然后逐格描述；
- 「请连起来理解整个视频在讲什么、大致讲了哪几件事」——把注意力从"画面里有什么"拉到"视频讲了什么"；
- 「用你自己的话说内容，别一格一格描述画面里有什么」——挡住"第 8 格有个蓝发女孩"这种回答；
- 「别提截图、宫格、抽帧、转写这些词」——别把工具痕迹露给用户；
- 「只给你看这一次，没看清就说没看清」——防止后面追问时编细节。

插件不规定输出格式（不强制"先总结再评论"），那是人格的事；插件只保证内容送到、且别被当成一堆逐格图片描述。

## 5. 函数清单

| 函数 | 作用 | 失败行为 |
| --- | --- | --- |
| `raw_link(event)` | 从消息文本里认裸 `BV` / `av` 号，转成 B 站视频 URL；没带链接也能看 |
| `share_link(comp, marks)` | 从 `Json` 组件拆出分享链接（默认只认 B 站） | 返回 `""` |
| `share_title(comp)` | 取标题（`desc`/`title`/`prompt`） | 返回 `""` |
| `site_marks(extra)` | 默认域名 + 配置 `extra_sites` 合并去重 | 至少含默认两个域名 |
| `chain_link(event, recent, marks)` | 依次从事件属性、当前链、引用链、缓存找链接 | 返回 `""` |
| `locate_ffmpeg(cfg)` | 配置 → PATH → 随包 `imageio-ffmpeg` → `C:\ffmpeg\bin` | 返回 `""` |
| `run_cmd(args, timeout)` | 异步跑子进程（Windows 不弹窗） | 返回 `(code, output)` |
| `media_info(ffmpeg, path)` | ffprobe 拿 `(时长, 有无音轨)` | 兜底解析 `ffmpeg -i` 的 stderr |
| `sample_times(duration, limit, count)` | 在 10%~90% 区间均匀取点 | 至少 1 个点 |
| `extract_frames(...)` | 每个时间点截一张 jpg（1280 宽） | 返回已截到的帧 |
| `run_cmd_bytes(args, timeout)` | 同 `run_cmd`，但收原始字节（rawvideo） | 返回 `(code, b"")` |
| `_rgb_to_hsv(rgb)` | numpy 向量化 RGB→HSV，色相/饱和/明度归一 0..1 | 无 |
| `scan_motion(ffmpeg, path, dur, clip)` | 4fps 缩到 64×36，逐帧算画面变化分（色相环形距离 + 按较低饱和度降权） | 返回 `[]` |
| `segment_by_motion(scores, dur, cells)` | 按累计变化量把时间轴等分成 cells 段 | 返回 `[]` 或等长段 |
| `board_layout(cells, aspect)` | 格数 → (列,行,画布宽高,格宽高)，整图 ≤640k 像素 | 无 |
| `fit_board_cells(want, aspect)` | 在想要格数以内挑一个每格 ≥240×135 的格数 | 保底 1 格 |
| `pick_cells(scores, want)` | 按 p90/中位数比把格数自适应到 6 / 9 / want | 无 |
| `make_visual(ffmpeg, path, dur, clip, conf, work)` | 画面这一段的总入口：宫格 / 逐帧两分支，返回 `{images, hint, frames, board, tokens}` | 返回空 `images` |
| `image_tokens(w, h)` | 按 14px patch 粗估一张图的 token（DSH 口径，只作量级参考） | 无 |
| `frame_sharpness(path)` | 拉普拉斯方差当清晰度 | 缺 PIL/numpy 返回 0 |
| `pick_sharp_frame(...)` | 一段里取 3 个候选点，留最清晰的一张 | 返回 `None` |
| `compose_board(frames, out, cells)` | PIL 拼图：cover 裁切、4px 间隙、左上角烧编号 | 返回 `False`（没装 PIL 等） |
| `storyboard_visual(ffmpeg, url, work, conf)` | 下载失败兜底：B 站官方故事板缩略图拼宫格 | 返回空 `images` |
| `fetch_desc(url, cookie)` | 取视频简介（`x/web-interface/view` 的 `data.desc`），截到 300 字 |
| `fetch_bili_subtitle(url, cookie)` | 直连 B 站 `player/v2` 取字幕，多轨重试 + 串片/中文占比校验 | 返回 `("", 原因)` |
| `fetch_subtitle(url, work, cookie)` | 有 cookie 先走 B 站接口，拿不到再 yt-dlp（json3/srt/vtt） | 返回 `""` |
| `parse_subtitle(path)` | 字幕文件转纯文本（去时间轴、去重复行） | 返回 `""` |
| `cookie_header(raw)` | 裸 `SESSDATA` 自动补成完整 Cookie 头 | 返回 `""` |
| `classify_error(msg)` | 下载异常归类：`not_found`/`vip`/`too_big`/`network`/`other` | 返回 `"other"` |
| `grab_audio(...)` | 切 30 秒一段 16k 单声道 wav | 返回已切到的段 |
| `find_asr(home)` | 找 `sherpa-onnx-offline.exe`、模型、tokens | 返回空串 |
| `transcribe_local(...)` | 本机 SenseVoice 转写 | 返回空串 |
| `transcribe_api(wavs, base, key, model)` | 任意 OpenAI 兼容接口转写 | 返回空串 |
| `download(url, dir, ffmpeg, max_mb, cookie, max_height)` | yt-dlp 下载并校验 | 返回 `("", 失败原因)` |
| `_sweep()` | 清 20 分钟前的临时目录和过期缓存 | 吞异常 |

## 6. 配置项语义

| 键 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `watch_qq_card` | bool | true | 总开关 |
| `require_at_in_group` | bool | true | 群里是否要 @ 才处理（`entry` 组） |
| `card_wait_seconds` | int | 300 | 群里先发卡片、后 @ 时这张卡片等人的上限；记的时候带发送者，别人 @ 不算 |
| `daily_limit` | int | 0 | 同一发送者每天最多处理几轮；0 不限。命中缓存的轮次不计数 |
| `board_budget` | `640000` | 宫格整图像素预算，直接决定 token 量级 |
| `scan_fps` | `4` | `scan_motion` 的采样帧率，越高越细也越慢 |
| `clip_seconds` | int | 300 | 只看前 N 秒 |
| `frame_style` | string | board | 画面呈现：宫格（省 token）/ 逐帧 |
| `board_cells` | int | 9 | 宫格格数上限（按起伏自适应到 6~want，再按 640k 预算下压） |
| `pick_sharp` | bool | true | 每段挑最清晰的一帧；关掉只取段中点 |
| `frame_count` | int | 6 | 截几张画面 |
| `size_limit_mb` | int | 120 | 下载大小上限 |
| `sweep_minutes` | int | 20 | 临时文件保留分钟数 |
| `ffmpeg_bin` | str | "" | 留空自动找 |
| `prefer_subtitles` | bool | true | 先试视频自带字幕，拿到就跳过转写 |
| `asr_mode` | str | off | `off` / `local` / `openai` |
| `local_asr_home` | str | "" | 本机识别目录 |
| `remote_asr_base` | str | `https://api.siliconflow.cn/v1` | 兼容接口地址 |
| `remote_asr_key` | str | "" | 接口 Key |
| `remote_asr_model` | str | `FunAudioLLM/SenseVoiceSmall` | 模型名 |
| `bili_cookie` | str | "" | 可选 Cookie；只填 SESSDATA 值也行 |
| `prompt_template` | str | "" | 自定义引导语，留空用内置；`$title`/`$length`/`$cut`/`$shot`/`$sound` 占位 |
| `extra_sites` | str | "" | 额外允许的域名，逗号分隔 |
| `include_desc` | `true` | 带上视频简介 |

## 7. 临时文件与清理

- 根目录：`get_astrbot_temp_path()/bili_card`，即
  `C:\Users\<你>\.astrbot_launcher\instances\<实例>\core\data\temp\bili_card\`
  （Linux 是 `<AstrBot>/data/temp/bili_card/`）；
- 每条视频一个 `<uuid>` 子目录：`video.mp4`、`frames/f00.jpg…`、`audio/chunk00.wav…`；
- 视频下载完、抽帧后立刻 `os.remove`；音频转写完立刻整目录删；
- **画面必须留到模型读图那一刻**——AstrBot 是在真正发请求时才读图片文件，
  所以不能在注入前删（早期版本就是在这里翻车，报 `FileNotFoundError`）；
- 所有子目录超过 `sweep_minutes`（默认 20 分钟），在下次处理时被 `_sweep()` 清掉；插件刚启动时也清一次；
- 卸载插件后这个目录可以整删。

## 8. 文字来源：字幕优先 + 三种转写模式

先看 `prefer_subtitles`（默认开）：

- 用 yt-dlp 拉视频自带字幕（UP 主上传的、或 B 站 AI 字幕），
  `subtitleslangs` 依次试 `zh-Hans / zh-CN / zh / ai-zh / zh-TW / zh-Hant`，
  格式优先 json3，其次 srt / vtt；
- 解析：json3 走 `events[].segs[].utf8`，srt/vtt 逐行去掉序号与时间轴；
- 拿到就**直接用它当"视频里说的话"**，不再跑转写（字幕最准、零成本）；
- 拿不到（`fetch_subtitle` 返回空）再走下面的 `asr_mode`。

注意：B 站匿名访问大多只能看到 `danmaku`（弹幕）字幕，**拿不到中文/AI 字幕**；
填了 `bili_cookie` 命中率会高一些，但仍取决于视频本身有没有字幕。

`asr_mode` 三档：

1. **off**（默认）：只要画面。零外部依赖，零额外费用。
2. **local**：本机 sherpa-onnx + SenseVoice 模型，参数
   `--sense-voice-model=<model.int8.onnx> --tokens=<tokens.txt> --sense-voice-language=zh --sense-voice-use-itn=true`，
   输出一行 JSON，取 `"text"`。不联网、不计费，速度约 1 秒/30 秒音频。
3. **openai**：任何 **OpenAI 兼容的 `/audio/transcriptions`** 接口。`remote_asr_base` 填到 `/v1` 为止，
   插件拼成 `<base>/audio/transcriptions`，`multipart/form-data` 上传 wav，读 `text` 字段。

第 3 种是通用扩展点，覆盖但不限于：

- 硅基流动：base `https://api.siliconflow.cn/v1`，model `FunAudioLLM/SenseVoiceSmall`
- OpenAI：base `https://api.openai.com/v1`，model `whisper-1`
- Groq：base `https://api.groq.com/openai/v1`，model `whisper-large-v3`
- 本机 faster-whisper / whisper.cpp 的 OpenAI 兼容服务：base `http://127.0.0.1:<port>/v1`，key 随便填

想接别的（阿里云、腾讯云、讯飞）就再加一档 `asr_mode`，实现一个 `transcribe_xxx()` 即可，
注入部分的代码完全不用动。

## 9. B 站 Cookie 策略

- **默认不填**：匿名访问大多数视频能拿到 360P/480P，看画面够用；
- **需要填**：大会员 1080P、登录可见视频、触发风控；
- 实现：填了 `bili_cookie` 时给 yt-dlp 加 `http_headers = {"Cookie": ..., "Referer": "https://www.bilibili.com/"}`；
- 不填就完全不发 Cookie 头，保持匿名；
- Cookie 只存在本地插件配置里，不写日志、不进对话、不上传。

## 10. 失败路径与降级

| 情况 | 行为 |
| --- | --- |
| 没装 yt-dlp | 返回 `no_ytdlp`，只给标题提示 |
| 没找到 ffmpeg | 本轮不处理，只给标题提示 |
| 下载失败 / 短链失效 | `classify_error` 归成 `not_found`/`vip`/`too_big`/`network`/`other`，按类给提示；46 字节 JSON 错误页靠大小+扩展名+文件头挡掉 |
| 下载失败但有官方故事板 | `storyboard_visual` 取 `videoshot` 缩略图拼宫格，附「没下载成功」说明；故事板也拿不到才只给标题 |
| B 站字幕接口抽风 | 空/串片/英文串片会被换轨重试（最多 3 条）并做中文占比检查，整体失败自动落到转写 |
| 视频超过 `size_limit_mb` | 放弃，只给标题（归到 `too_big`） |
| ffprobe 失败 | 兜底解析 `ffmpeg -i` 的 stderr |
| 截帧失败 | 注入"没截出来，别编" |
| 转写失败 | 只看画面，日志记原因 |
| 群聊没 @ | 只记卡片，不下载 |

## 11. 实测成本与性能

在一台 Ryzen 7 8845HS / 16G 的机器上（2026-10-31 实测）：

- 逐帧模式：7 分 20 秒视频、26MB：下载 5.9 秒，6 帧 + 1558 字转写，合计 18.2 秒；
- 3 分 32 秒的视频、9MB：下载 5 秒，6 帧 + 1103 字转写，合计 14.6 秒；
- 下载失败兜底：无效短链 1 秒内返回，不产生误报；
- 字幕：`BV1Vzxc6dEwp` 匿名 `--list-subs` 只有 `danmaku`，`fetch_subtitle` 返回空串（正常，不抛错）；
- 帧大小约 14~106KB，6 张图约一张图的钱量级。

### 宫格（frame_style=board）实测

- 同一条 3 分 32 秒视频（`BV1Vzxc6dEwp` 那个短链）：480P 下载 6.9 秒（7.3MB）→ `scan_motion` 719 个采样点 / 4.8 秒 →
  9 段取帧加拼图 1.5 秒，单轮约 25 秒，换来的是图数量从 6 张变 1 张；
- 布局：9 格 → 3×3、画布 `1066×598`、每格 `350×194`（`scene` 档，能认出人和大字，认不清小字幕）；
- 格数自适应：同一条视频 p90/中位比约 6~7 → 用满 9 格；纯静态画面（<2）会降到 6 格，拼图省 1/3 的图；
- 清晰度选帧：每段取 25%/50%/75% 三张候选，拉普拉斯方差 2274~3391 之间有明显差别，选中的偶尔是 25%/75% 位；
  9 段并发（信号量 4）多花约 1.5~2 秒，换来避开转场和运动模糊；
- token 粗估（DSH 口径 `ceil(w/42)*(ceil(h/42)+1)+2`）：宫格 **418** vs 6 张 720p **3,546**，省约 88%；
  API 宿主按自己的图片计价，口径不同，这里只作数量级参考；
- 并行：字幕（网络）与 `make_visual`（本地解码）同时起，比串行少 2~3 秒；
- 失败分类：无效短链归 `not_found`、会员视频归 `vip`、超大小归 `too_big`、超时归 `network`，提示语不同但都只给标题；
- 日志尾部带「约 N token」，方便看每条视频花了多少图钱。

## 12. 和同类插件的关系

- 本插件是**从零写的独立实现**，没有复制 `astrbot_plugin_dafeiyu_video` 的代码；
- 配置项名、函数名、日志措辞都刻意避开，避免看起来像改版；
- 与 BiliRead 类插件的区别：那条路读字幕→第二模型总结；这条路把画面直接喂给正在对话的模型。
- 宫格 + 时间清单的思路参考 [`SAOS-3624/dsh-video-storyboard`](https://github.com/SAOS-3624/dsh-video-storyboard)（Apache-2.0）；只借鉴工程做法，代码自写，未复制其源码。

## 13. 发布检查清单

- [x] `metadata.yaml` 字段齐全（name / display_name / desc / version / author / repo / astrbot_version / support_platforms）
- [x] `LICENSE`（MIT）
- [x] `README.md`
- [x] `requirements.txt`（yt-dlp / numpy / Pillow）
- [x] `.gitignore`（排除 `__pycache__`）
- [ ] `repo` 填 GitHub 地址
- [ ] 推仓库
- [ ] [cloud.astrbot.app/publish](https://cloud.astrbot.app/publish) 提交 zip

## 14. 边界约定（改动前必读）

这一节不是现状描述，是**目标**与**判据**。任何改动都要往这里靠；靠不上就先说清楚为什么。

### 14.1 五条边界

| 边界 | 唯一入口 | 管什么 | 现状 | 违反的表现 |
|---|---|---|---|---|
| 网络 | 目标 `net` 模块 | 所有出网请求 | 分散在 `download`、`fetch_bili_subtitle`、`fetch_desc`、`transcribe_api` 等处 | 同一件事在多处设超时；某处忘了带/忘了不带 Cookie |
| 子进程 | 目标 `proc` 模块 | 所有外部程序 | `run_cmd`、`run_cmd_bytes` 已收口，但 `download` 里 yt-dlp 走库、`transcribe_local` 直接 `run_cmd` | 出现 `shell=True`、没有超时、可执行文件路径来自消息内容 |
| 注入 | `_say` + `_run` 里三处 `ImageURLPart` | 塞给模型的一切内容 | `_say` 已统一，图片三处分散 | 少了 `.mark_as_temp()`，内容永久留在会话历史 |
| 配置 | `self._get()` | 读配置 | 已统一，但无启动自检 | 拼错键名后静默用默认值，排查半天 |
| 观测 | 日志 | 每阶段耗时、失败原因 | 有总耗时与三段耗时，缺节点级信息 | 只知道"慢"，不知道慢在哪一步 |

### 14.2 网络

出网只到 B 站公开接口（`x/web-interface/view`、`view/detail`、`player/v2`、`v2/subtitle/web/view`、`player/videoshot`、`v2/reply`）与使用者自己配置的转写/模型地址；`v2/reply` 只读正文，不带昵称。

- 请求只发往白名单：`BILI_MARKS`（`b23.tv`、`bili2233.cn`、`bilibili.com`）与用户配置的 `extra_sites`，逐个过 `bad_host()` 拒内网；
- Cookie 只经 `bili_cookie(url, cookie)` 发出，非 B 站域名返回空串——这是市场审核指出的洞，不许回退；
- 超时、重试、UA、Referer 集中在入口一处设置，不在各调用点各写一份；
- 下载只取前 `clip_seconds` 秒（`download_ranges`）：画面只看前几分钟，整条下完是白花时间；
  截断后 ffprobe 量到的是片段长度，真实时长由 `download()` 单独返回，字幕校验按真实时长算；
- 节点连不上要快失败：超时 5 秒、重试 1 次，然后交给故事板兜底，别在死节点上白等一分钟。

### 14.3 子进程

- 只允许 `ffmpeg`、`ffprobe`、`yt-dlp`、`sherpa-onnx-offline` 四类可执行文件；
- 参数用列表传，禁止 `shell=True`，禁止把消息里的原文拼进命令行；
- 必须带超时；返回值统一走 `(code, text)`。

### 14.4 注入

- 说明文字走 `_say()`，图片走 `ImageURLPart(...).mark_as_temp()`；
- 任何注入内容必须是临时的（只管这一轮），不许写进会话历史；
- 新增注入点时，先问一句"这条内容该活多久"，答完再写。

### 14.5 配置

- 读取一律带默认值（`self._get(key, 默认)`）；
- 改动后必须能在 `_conf_schema.json` 里找到同名项，否则等于隐藏开关；
- 启动自检清单：`bili_cookie` 是否填、`local_asr_home` 路径是否存在、`ffmpeg_bin` 是否有效、`extra_sites` 是否合法。

### 14.6 观测

每个阶段至少一行日志，失败必带原因分类（`classify_error` 的取值域）。当前阶段：

```
收到卡片 / 记下卡片
下载完成：大小 + 秒数        ← 新增
下载失败：原文 + 失败节点      ← 新增（节点名由 cdn_hosts 提取）
字幕：拿到多少字 / 为什么不能用（mismatch、覆盖不足、empty）
画面：宫格格数 + token + 秒数
声音：转写字数 + 秒数
总账：下载 x/画面 x/声音 x/总计 x
```

### 14.7 改动的固定顺序

1. 先跑 `tests/test_pure.py`，确认基线是绿的；
2. 先加日志，让问题可见，再改逻辑；
3. 一次提交只做一类事（修 bug 不夹带重构，重构不夹带功能）；
4. 改完再跑一次测试，并贴出实际输出，不靠"应该没问题"。

### 14.8 怎么算合格

- `python -m py_compile main.py` 通过；
- `tests/test_pure.py` 全绿；
- 真机跑一次「群里卡片 + @」「私聊链接」两条路径，日志里五个阶段都有数；
- `git diff` 里没有与本次目的无关的改动。

## 15. 支持的链接形态

| 形态 | 识别位置 | 处理 |
| --- | --- | --- |
| `.../video/BV1xx...` | `bili/link.py` 的 `URL_RE` + `_first_link`（`host_in` 精确/子域匹配） | 直接拿 BV |
| `b23.tv/<code>`、`bili2233.cn/<code>` | `bili/security.py` 的 `SHORT_HOSTS` | `bili/http.py` 的 `_bvid_of` 跟随重定向（HEAD 失败再 GET），结果存 `_BV_CACHE` |
| `av12345` / 裸 `BV` 号 | `bili/link.py` 的 `RAW_RE` | 直接拼成 `.../video/BV...` |
| `?p=N` | `bili/http.py` 的 `page_of` | BV 与 p 一起缓存（`_BV_CACHE: dict[str, tuple[str, int]]`），`page_cid` 取 `pages[p-1]` 的 cid 和时长 |
| 番剧 `bangumi/play/ep...`、课程 `cheese/play/ss...` | `_bvid_of` 找不到 BV | 返回 `""` → `download` 直接 `return "", "not_found", 0.0`（约 0.8 秒）→ `storyboard_visual` 兜底 |

番剧不支持下载是刻意的判断，不是遗漏：`ep` / `ss` 链接里没有 BV 号，得另走
`pgc/view/web/season?ep_id=` 取 cid、`pgc/player/web/playurl?ep_id=` 取流，而付费集有
「试看 6 分钟」「只放 30 秒」几种残缺形态，接进来容易把半截当全片。现在的行为是快速失败 +
官方故事板缩略图，日志里归为 `not_found`，不会被误判成 `network` 反复重试。

新增链接形态时，同时改：`bili/security.py` 的 `BILI_MARKS`/`SHORT_HOSTS`、`bili/link.py` 的
`URL_RE`/`url_key`、`bili/http.py` 的 `bv_cache_key`/`_bvid_of`，并在 `tests/test_pure.py` 的
`case_link` 里补断言。


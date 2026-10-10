# LinkWork · 链接文案提取器

在浏览器右侧边栏粘贴一条链接，自动提取**视频口播稿 / 图文文案 / 网页正文**，一键复制。

架构：**浏览器插件（界面与取数）+ 本地 Python 服务（语音识别）**。

> **运行环境**：macOS（图片 OCR 与系统语音识别依赖 Apple Vision / Speech 框架）· Python 3.10–3.13 · Edge 或 Chrome（MV3）
> **当前版本**：0.2.14　|　**许可**：[MIT](LICENSE)

| 层 | 负责 | 为什么这样分 |
| --- | --- | --- |
| 浏览器插件 | 读取页面数据、拿媒体地址、显示与复制 | 只有浏览器里才带着真实登录态、指纹与站点自己算好的请求签名。抖音的 `a_bogus` / Argus 校验无法在浏览器外重放（实测：即使带上 121 条登录 cookies，接口仍 403 `Blocked by ArgusSecurityPlugin`） |
| 本地 Python 服务 | 语音识别、图片 OCR、文本规范化、导出 | Whisper 模型与 ffmpeg 放浏览器里不现实；这部分能力已经过实测验证 |

---

## 1. 快速开始（三步）

```bash
cd LinkWork

# ① 安装依赖（首次，自动建 .venv，含自带 ffmpeg 与 faster-whisper）
./scripts/setup.sh
```

```bash
# ② 启动本地服务（插件依赖它，后台常驻）
./scripts/service.sh start
#    ./scripts/service.sh status    查看状态
#    ./scripts/service.sh stop      停止
```

```bash
# ③ 安装浏览器插件（复制文件 + 打开 Edge 扩展页）
./scripts/install_extension.sh edge
```

第 ③ 步会打开 Edge 的扩展管理页，**还需要你手动点两下**（macOS 未授予终端"辅助功能"权限，这一步无法自动化）：

1. 打开扩展页右上角的 **开发人员模式**
2. 点 **加载解压缩的扩展**，选择目录：
   `~/Library/Application Support/LinkWork/extension`

装好后点击工具栏的 **LinkWork** 图标，右侧即打开提取面板。

> 也可以直接双击项目根目录的 `启动 LinkWork.command`，它会自动启动服务、同步插件文件并打开扩展页。

---

## 2. 侧边栏怎么用

面板从上到下三块：

1. **分享链接** —— 粘贴视频/图文/网页链接，点「提取文案」。支持 `Cmd+Enter` 快捷提取。
   （链接请从浏览器地址栏或分享功能复制后粘贴；扩展不会读取你当前打开的页面。）
2. **提取结果** —— 提取到的文案显示在这里，上方会标注平台、方式、字数、时长。
3. **运行日志** —— 折叠区，出错时可展开看具体原因。

复制按钮有两个：**复制文案**（整理后的正文）和**复制原文**（未整理的原始文本）。

按内容类型自动分流：

| 链接类型 | 处理路径 |
| --- | --- |
| 视频（抖音/B站/小红书视频/YouTube） | 后台读取页面媒体地址 → 本地服务下载 → 语音识别 → 口播稿 |
| 图文笔记（小红书图文） | 读取笔记正文 + 图片直链 → 本地 Vision OCR → 合并文案 |
| 网页/公众号文章 | 读取正文 DOM → 规范化整理 |
| 直接粘贴的文案 | 直接整理 |

**失败即报错，不做降级猜测**：拿不到就说清原因（例如"未登录抖音"、"媒体地址过期"、"页面不是详情页"）。

---

## 3. 技术要点与实测结论

### 取数策略：JSON 优先，且对改版有兜底

插件在页面里取数的优先级：

| 优先级 | 来源 | 抗改版能力 |
| --- | --- | --- |
| ① | `_ROUTER_DATA`（分享页/详情页，服务端注入 JSON） | 较高 |
| ② | `RENDER_DATA`（精选/推荐页，URL 编码 JSON，字段分散 → 深度聚合） | 较高 |
| ③ | 已知容器键名：`videoInfo` / `videoInfoRes.item_list` / `aweme_detail` | 中（键名会变） |
| ④ | **通用媒体扫描**：不按键名，遍历整棵 JSON 树找「形似媒体」的 URL | **对键名变化鲁棒** |
| ⑤ | DOM 选择器（`[data-e2e=...]`）、`<video>.currentSrc` | 较低 |

④ 是专门为"抖音改键名"准备的兜底：即使 `play_addr` 改名成任意名字，
只要 URL 仍落在 douyinvod / snssdk / playwm / aweme/v1/play 等特征上就能被扫到，
同时按 URL 特征排除封面图（`douyinpic`、`.jpeg` 等）。

**日志里的排查信息**（侧边栏「运行日志」）：

```
取数来源: _ROUTER_DATA ✅ / RENDER_DATA ❌ / DOM选择器 ⚠️部分 / <video> ❌
媒体来源: JSON 媒体字段
页面数据块: _ROUTER_DATA, videoInfoRes
data-e2e 取值: feed-video, video-desc
```

改版时看这几行就能定位是哪一层失效；日志区还提供「复制日志」按钮，方便反馈排查。

> 说明：推荐流首页（`douyin.com/?recommend=1`）的服务端数据里 `playAddr` 为空数组、
> `playApi` 为空字符串，媒体地址由前端另行请求（带签名）。因此在推荐流页面上只能拿到
> **视频文案（作者描述）**，拿不到口播稿。要口播稿请用视频详情页链接或分享短链。

### 为什么不用 Python 直接抓抖音

实测记录（本机 Edge 154）：

| 尝试 | 结果 |
| --- | --- |
| yt-dlp 读取 Edge cookies | ✅ 成功取出 2527 条，其中抖音域 **121 条**，含 `sessionid`/`ttwid`/`s_v_web_id`/`UIFID` 完整登录态 |
| 带这些 cookies 请求 `aweme/detail` | ❌ **403** `Blocked by ArgusSecurityPlugin Uifid Not Found` |
| 带这些 cookies 请求 `aweme/post` | ❌ 同上 403 |
| 旧接口 `iteminfo` | ❌ `status_code: 11110, encrypt_data_miss`（现在要加密请求体） |
| yt-dlp 自带抖音提取器 | ❌ 源码里就写着 `# TODO: Run verification challenge code to generate signature cookies` |

**结论：登录态能读到，但请求签名（`a_bogus` / Argus）是浏览器 JS 运行时算的，无法在 Python 侧重放。** 所以正确做法是在浏览器里取数——这正是插件的价值。

### 图片 OCR 的一个关键坑

macOS Vision 的 **accurate 模式（`recognitionLevel=1`）对中文经常直接返回空**，而 **fast 模式（`=0`）能正确识别中文**（用百度 logo 图验证：「Bai du 百度」）。

因此 OCR 采用两轮策略：先 fast，有效字符不足 4 个再用 accurate 复跑择优。实测识别 Safari 渲染的中文页面截图，逐行正确（含中英混排）。

### 识别链路的三个保护（沿用此前实测修复）

1. **音轨可能是静音的**：按码率从高到低重试，转成 WAV 后测峰值/RMS，静音则换下一条音轨。
2. **VAD 会把整段音频判为非语音**（MV/BGM 类）：识别覆盖时长 < 15% 时自动关闭 VAD 重跑（实测从 0 段恢复到 50 段）。
3. **繁简与标点**：统一繁转简（opencc）+ 全角标点 + 中英文间补空格；**换行会被保留**（否则图文段落会被合并）。

### 本地服务的安全边界

- 只绑定 `127.0.0.1`；
- 严格来源校验：拒绝所有 `http(s)://` 来源，只允许扩展来源与无 Origin 的本地调用——网页 JS 无法驱动本服务；
- 不引入令牌机制（本地工具，靠来源校验 + 自定义头即可，减少用户操作）。

---

## 4. 目录结构

```
LinkWork/
├── extension/                  # 浏览器插件（Edge / Chrome，MV3）
│   ├── manifest.json
│   ├── background.js           # service worker：侧边栏、调用本地服务、页面取数
│   ├── sidepanel.html/.css/.js # 右侧边栏界面
│   └── icons/                  # 插件图标
├── src/linkwork/
│   ├── service.py              # 本地 HTTP 服务（插件后端）
│   ├── jobs.py                 # 后台任务队列与状态
│   ├── cache.py                # 媒体缓存目录（统计 / 清理 / 启动自愈）
│   ├── transcribe.py           # 媒体 → 口播稿（静音检测 + VAD 兜底）
│   ├── media.py                # ffmpeg 定位、下载、字幕解析
│   ├── audio_decode.py         # 绕过 PyAV 版本不兼容的 WAV 解码层
│   ├── textnorm.py             # 繁简转换、标点规范化
│   ├── ocr.py                  # macOS Vision 图片文字识别
│   ├── exporters.py            # Markdown / TXT / JSON / SRT 导出
│   ├── pipeline.py             # 链接 → 结果 的编排
│   ├── asr/                    # faster-whisper 与 Apple Speech 引擎
│   └── resolvers/              # 各平台解析器（B站/抖音/小红书/YouTube/网页/通用视频）
├── scripts/
│   ├── setup.sh                # 安装依赖
│   ├── service.sh              # 本地服务启停（start/stop/status/restart）
│   ├── install_extension.sh    # 引导在浏览器中加载插件
│   ├── check_extension.py      # 插件/服务状态检查，--reload 触发重载
│   ├── verify_extension_browser.py  # 浏览器级验证（--worker / --panel）
│   ├── bump_version.py         # 统一递增版本号
│   └── make_icon.py            # 生成插件图标
├── tests/                      # 单元测试 + 服务 API 集成测试
└── requirements.txt
```

---

## 5. 本地服务 API（插件调用）

服务默认 `http://127.0.0.1:17890`，全部接口需带自定义头（网页无法伪造），返回 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务状态与可用 ASR 引擎 |
| POST | `/api/resolve` | 按链接解析（服务端自行下载，异步任务） |
| POST | `/api/transcribe` | 提交媒体（base64 或 `media_url`）→ 语音识别（异步任务） |
| POST | `/api/ocr` | 提交图片直链列表 → 图片文字识别（异步任务） |
| POST | `/api/format` | 整理页面文本，同步返回 |
| GET | `/api/jobs/{id}` | 查询任务状态与结果 |
| GET | `/api/cache` | 查看媒体缓存占用 |
| POST | `/api/cache/clear` | 清除已下载的媒体与中间产物 |

耗时接口返回 `{job: {...}}`，随后轮询 `/api/jobs/{id}` 直到 `state` 为 `done` 或 `error`。

---

### 怎么拿到诊断信息

出问题时，三种方式任选（内容相同）：

**① 一键脚本（最省事）**

```bash
.venv/bin/python scripts/diagnose.py
```

会打印可读摘要，并把完整 JSON 存到 `output/diagnostics.json`——把这个文件发我即可。

```bash
.venv/bin/python scripts/diagnose.py --json          # 直接打印完整 JSON
.venv/bin/python scripts/diagnose.py --log <文件名>  # 看某个任务的完整日志
```

**② 浏览器**（JSON 会格式化显示，方便直接复制）

```
http://127.0.0.1:17890/api/diagnostics
```

**③ 任务日志文件**（想看最原始的内容）

```
~/Library/Caches/LinkWork/tasklogs/transcribe_<任务ID>.log
```

### 诊断接口（排查问题用）

| 接口 | 作用 |
| --- | --- |
| `GET /api/diagnostics` | 返回版本、运行环境、日志目录、**最近任务的结果摘要（成功/失败+原因+阶段耗时）**、服务日志尾部 |
| `GET /api/diagnostics/log?name=<文件名>` | 读取某个任务的完整诊断日志 |

**每个任务都会单独落一份日志**到 `~/Library/Caches/LinkWork/tasklogs/`，包含：

- 运行环境快照（版本、Python、平台、ffmpeg 路径、可用 ASR 引擎）
- 任务输入（**cookie/token 等已脱敏**，只记长度）
- 各阶段耗时与细节：媒体域名、HTTP 状态、Content-Type、下载大小、
  ASR 输入规格、子进程 PID、识别进度里程碑、分段数与字数
- 失败时的完整异常栈

日志行格式为 `[墙钟时间 +相对耗时] 内容`，父进程与识别子进程写的是同一份文件，
因此带墙钟时间才能判断真实先后顺序。

`/api/diagnostics` 的返回里，每个任务都带 `outcome`（`done`/`error`）、`detail`（结果或失败原因）
与 `stages`（各阶段耗时），可以**一眼看出哪个任务失败、失败在哪一步**，不必逐个打开日志：

```json
{
  "summary": { "recent": 3, "failed": 1 },
  "logs": [
    { "name": "transcribe_9e20….log", "outcome": "error",
      "detail": "LinkWorkError: 媒体下载失败：HTTP 404（可能链接已过期）" },
    { "name": "transcribe_916e….log", "outcome": "done",
      "detail": "✔ 完成，耗时 9.34s",
      "stages": ["下载媒体@0.1s", "提取音频@0.5s", "语音识别@5.0s"] }
  ],
  "service_log_tail": ["…"]
}
```


## 6. 媒体缓存与空间占用

下载的媒体与识别中间产物统一放在：

```
~/Library/Caches/LinkWork/media
```

**三层控制，默认就不会占空间：**

1. **任务结束即清理**：每次提取完成后，该次的工作区（媒体文件 + 转码 WAV / OCR 图片）立即删除，
   不会因为"用完忘了删"而堆积。
2. **侧边栏一键清除**：面板底部显示当前占用（如 `3.5 MB · 2 个目录`），
   点「清除缓存」即可全部释放；旁边还有「提取后自动清理」开关（默认开启）。
3. **启动自愈**：服务启动时会清理上次异常退出遗留的孤儿目录（超过 6 小时的才算孤儿，
   以免误删正在运行的任务）。

**为什么选 `~/Library/Caches`**：这是 macOS 认可的缓存位置——系统空间紧张时可以被安全回收，
符合"这是可再生数据"的语义。想改位置可以设置环境变量：

```bash
LINKWORK_CACHE_DIR=/path/to/dir ./scripts/service.sh restart
```

> 关于"浏览器清缓存时一起清掉"：扩展的媒体**不在浏览器缓存里**，它由本地 Python 服务落盘，
> 所以 Edge 的「清除浏览数据」不会碰到它（这其实是好事：不会误删正在用的中间文件）。
> 实际效果等价：`~/Library/Caches/LinkWork` 会被系统缓存清理工具纳入回收范围，
> 加上上面的任务级自动清理和手动按钮，日常不会堆积。

**相关接口**：`GET /api/cache` 查看占用，`POST /api/cache/clear` 清除。

---

## 7. 命令行用法（调试用）

```bash
.venv/bin/linkwork doctor                 # 环境自检
.venv/bin/linkwork serve                  # 启动本地服务（前台）
.venv/bin/linkwork "视频或文章链接"         # 直接提取（不开浏览器）
.venv/bin/linkwork --format md,txt,json,srt -o ~/Desktop/文案 "链接"
.venv/bin/linkwork --model base "链接"     # 换更小的模型（更快）
```

---

## 8. 开发与验证工具

`scripts/` 下除安装/启停脚本外，还有几个自检工具：

| 脚本 | 用途 |
| --- | --- |
| `check_extension.py` | 检查插件文件与本地服务是否就绪；`--reload` 触发浏览器重载扩展 |
| `verify_extension_browser.py` | 在真实 Edge（临时配置，不动你的登录态）里验证：`--worker` 进 service worker 上下文查后台健康度，`--panel` 渲染侧边栏页面 |
| `make_icon.py` | 由代码生成插件图标（不依赖 Pillow） |
| `bump_version.py` | 统一递增版本号（见下文「版本管理」） |
| `diagnose.py` | 一键查看诊断：`.venv/bin/python scripts/diagnose.py`（`--json` 输出完整 JSON，`--log 名称` 看单个任务日志） |

> 说明：临时配置 + `--remote-debugging-port` 是唯一能在不改动你浏览器配置的前提下做浏览器级验证的方式；
> 默认用户目录会拒绝开启远程调试（Edge 限制）。

---

## 9. 版本管理

**规则：每改动一次代码，递增一次版本号。**

单一来源是仓库根目录的 `VERSION` 文件，其余位置由脚本同步：

```bash
.venv/bin/python scripts/bump_version.py          # 补丁号 +1（0.2.1 → 0.2.2）← 日常改动用这个
.venv/bin/python scripts/bump_version.py --minor  # 次版本 +1（0.2.1 → 0.3.0）
.venv/bin/python scripts/bump_version.py --major  # 主版本 +1（0.2.1 → 1.0.0）
.venv/bin/python scripts/bump_version.py --set 0.4.0   # 直接指定（不消耗 build 号）
```

一次命令同步四处：

| 位置 | 作用 |
| --- | --- |
| `VERSION` | 版本唯一来源 |
| `pyproject.toml` | Python 包版本 |
| `src/linkwork/__init__.py` | `__version__`，`/api/health` 会返回 |
| `extension/manifest.json` | 浏览器扩展版本（`edge://extensions` 上可见） |
| `extension/extractor.js` | `EXTRACTOR_BUILD`，侧边栏日志里显示 |

后两项让你能**一眼确认浏览器里跑的是不是最新代码**——日志里的 `build=N` 和扩展页的版本号对不上，就说明需要点「重新加载」。

`tests/test_core.py::test_version_is_consistent_everywhere` 会校验四处一致，防止只改一处。

---

## 10. 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

覆盖内容：解析器分派、字幕解析（VTT/SRT/json3）、B站 WBI 签名、导出格式、音频静音检测、
文本规范化、Vision OCR 双模式策略，以及**本地服务的全部 API**（含来源限制、任务轮询、真实音频转写、真实中文图片 OCR）。

---

## 11. 已知限制

- **抖音**：未登录时页面不返回文案，插件会明确提示需要登录。登录后由插件在页面里取数，才能拿到 `play_addr`。
- **小红书**：笔记页需要登录态；纯图片无文字的笔记只能拿到标题与话题。
- **视频号**：通常需要登录态。
- **媒体地址有时效**：必须在页面上打开视频后再提取，链接过期会报"媒体下载失败"。
- 部分平台（36氪等）正文依赖前端渲染，网页抽取可能拿到导航文案。

---

## 12. 免责声明

本工具仅供个人学习与内容整理使用。请在遵守各平台服务条款与著作权规定的前提下使用，
不要用于批量抓取或商业转载。

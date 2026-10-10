# LinkWork → 扣子（Coze）插件 / 工作流：实施计划

> 状态：**计划阶段，未开始开发**。本文只描述方案、边界、里程碑与验收标准。
> 约束（来自需求）：不删除现有代码；只做扩展；不修改现有代码；浏览器插件与扣子插件两种形态必须能并存。
> 事实依据：`docs/coze/feasibility-report.md`（扣子平台侧，911 行，逐条附官方引文）；`extension/**` 与 `src/linkwork/**` 的只读代码审计。

---

## 0. 结论先行（v2，已按你的决策修订）

### 0.1 已确认的决策

| # | 决策 | 影响 |
| --- | --- | --- |
| D1/D2 | **抖音 / 小红书是核心场景，必须要口播稿** | **云侧插件（路线 A）出局**（架构性不可行，见 0.2）；主路线改为"本机网关 + 端插件" |
| D3 | `coze/` **自带独立版本号**，只在正式发布扣子插件时按仓库规则 bump | 开发期完全不触碰现有 5 个版本号文件 |
| D4 | **暂不执行 `git init`** | Phase 1 为纯新增目录，删目录即回滚，可接受；但见 0.4 的准入门槛 |
| D5 | 下一步 = **Phase 0 + Phase 1**（本地适配网关） | 见 §8 |

### 0.2 必须认清的硬事实：扣子拿不到抖音口播稿

抖音/小红书的媒体地址由**页面 JS 运行时**算好签名后注入 DOM/全局 JSON（`extension/extractor.js:446,638-644`；`extension/` 内 grep `bogus|signature|w_rid` **零命中**——扩展本身也不造签名，只是"读页面现成的"）。而扣子侧：

- 云侧插件只能发普通 HTTP，**不能持 Cookie、不能执行 JS**；
- 代码节点**禁用 `sockets`/`threading`**（硬约束 #9），跑不了真浏览器；
- 官方唯一带"浏览器"的 Scraper 是 **Chrome 手动采集扩展**，不是 API、不能在工作流里调用（硬约束 #15）。

**结论：无论插件部署在哪里，扣子云端自身都无法拿到需要登录态 + 浏览器签名的数据。** 这是路线选择的真正约束。

### 0.3 因此主推：路线 D —— 本机网关 + 端插件 + Playwright 持久登录通道（零改动）

关键发现：**现有取数逻辑可以脱离 Chrome 复用，且不需要改它**。

- `extension/extractor.js:692-694` 已把 `pageExtractor` 导出给 Node；
- `tests/extension_extractor.test.js:37-45,48,135-139` 已经用 DOM shim **在 Node 里直接跑 `pageExtractor()`** —— 官方证据表明这套解析逻辑（`_ROUTER_DATA`/`RENDER_DATA` 解析、`scanForMedia` 通用媒体扫描、`isRealMedia` 水印/封面排除、候选排序）**不依赖 Chrome API**，只依赖 `document`/`location`。

于是本机网关可以内置一个 **Playwright 持久化登录浏览器通道**：

```
扣子（端插件）→ 本机网关 :17891
                   │  Playwright 持久 profile（首次扫码登录一次，之后长期有效）
                   │  加载真实页面 → 注入【已存在的】extension/extractor.js（只读复用）
                   ▼
              拿到带签名的媒体直链（等价于浏览器插件取到的那个）
                   │
                   ▼
              现有服务端能力：下载 → 转码 → faster-whisper → 口播稿
```

- 走的是**真实浏览器 + 真实登录态**，页面自己的 JS 正常执行并算出签名 → **不绕签名**，因此风险远低于纯 HTTP 反推签名；
- **`extension/**` 与 `src/linkwork/**` 一行不改**，`extractor.js` 只被 `require`/注入，不被修改；
- 与现有浏览器插件**互不干扰**（不同 profile、不同进程，且可同时存在）。

**路线 D 的代价（如实列出）**：

| 代价 | 说明 |
| --- | --- |
| 首次登录需交互 | 网关要维护一个独立浏览器 profile，首次需扫码登录抖音/小红书（与日常浏览器分离） |
| 本机必须常驻 | Mac 开机 + 网关进程运行；扣子侧不可用时需有明确提示 |
| 端插件仅 API 可用 | 官方限制：端插件**页面内无法调试**、大模型节点不支持配置（硬约束 #10/#11）→ 只能在扣子 **API 方式**（`v3/chat` → `requires_action` → `submit_tool_outputs`）下使用 |
| 体积与资源 | Playwright + 浏览器运行时数百 MB（可指定复用系统 Edge/Chrome 以省下载） |
| 自动化风控 | 仍属自动化访问；已用真实登录态+真实页面把风险压到最低，但不能承诺 100% 不被风控 |
| 明确不做 | 不做验证码绕过、不做批量抓取、不做签名算法逆向 |

### 0.4 准入门槛（安全网）

- **Phase 1 为纯新增**（只创建 `coze/**`），删目录即回滚 → 在无 git 的情况下可以开工；
- **任何需要触碰 `extension/**`、`src/linkwork/**`、`scripts/**`、`tests/**` 现有文件的步骤，必须先建立安全网**（`git init` 或整目录快照备份），未建立前不得开工；
- 建议（非强制）在 Phase 2 动浏览器通道前补一次快照：`tar --exclude=.venv --exclude=output -czf ../LinkWork-snapshot-$(date +%Y%m%d).tgz .`（写入工作区外需你确认）。

---

## 1. 现状事实基线（代码证据）

### 1.1 两层职责

| 层 | 做什么 | 关键位置 |
| --- | --- | --- |
| 浏览器插件（MV3） | 侧边栏 UI、注入页面主世界取数、读 cookie、页面内 `fetch` 抓媒体字节、调用本地服务、轮询任务 | `extension/manifest.json:7-17`、`extension/background.js:96-144`、`extension/extractor.js:332-621` |
| 本地 Python 服务 | HTTP API、后台任务队列、链接解析、下载/转码、语音识别、OCR、文本规范化、导出 | `src/linkwork/service.py`、`jobs.py`、`pipeline.py`、`resolvers/**`、`asr/**` |

服务默认 `127.0.0.1:17890`（`src/linkwork/service.py:44-45`）。

### 1.2 「离了浏览器就拿不到」的致命清单

这些是**云端无法复刻**的，写进设计前必须先接受：

| 拿不到的东西 | 为什么 | 证据 |
| --- | --- | --- |
| Cookie（含 httpOnly） | 只能由浏览器 cookie store 提供 | `extension/background.js:271-278` |
| 页面算好的签名地址 | 扩展**只读不造**，全扩展无任何签名/加密代码 | `extension/extractor.js:446,638-644`；`extension/` 内 grep `bogus/signature/w_rid` 零命中 |
| 渲染后的 DOM 与注入 JSON | `_ROUTER_DATA` / `RENDER_DATA` / `__INITIAL_STATE__` 需执行过页面 JS | `extension/background.js:168-169,173-177`、`extension/extractor.js:334,341,521,544` |
| `<video>.currentSrc` | 媒体元素状态，且可能是 `blob:` | `extension/extractor.js:405,602-603` |
| 服务本身 | 只在回环地址；Coze 云端请求由 Coze 服务器发起，访问不到你的 `127.0.0.1` | `service.py:703-713`；`docs/coze/feasibility-report.md` 第 2 节 |

### 1.3 服务端可迁移性（决定云侧插件的能力清单）

| 模块 | 评级 | 依据 |
| --- | --- | --- |
| B站解析器 + WBI 签名 | ✅ 云端可用 | 匿名公开接口 + 纯 Python md5 签名（`resolvers/wbi.py:63-71`、`resolvers/bilibili.py:145-185`） |
| YouTube / 通用视频 / 直链 | ✅ 云端可用 | 全走 yt-dlp（`resolvers/youtube.py:20`、`resolvers/video.py:70-83`） |
| 网页正文 | ✅ 云端可用 | requests + BeautifulSoup（`resolvers/web.py:40-42,125-163`） |
| 抖音解析器 | ⚠️ 只剩降级 | 只有分享页 SSR/meta 兜底，**无任何签名实现**（`resolvers/douyin.py:46-85`；`src/` 内 grep `a_bogus/argus/msToken` 零命中） |
| 小红书解析器 | ⚠️ 需过登录墙 | SSR 解析可用但页面需登录；图文笔记强依赖 macOS OCR（`resolvers/xiaohongshu.py:198-204`） |
| faster-whisper ASR | ✅ 跨平台 | CPU/int8 自动降级，子进程用 `sys.executable`（`asr/faster_whisper_engine.py:60-68`、`asr/isolated.py:74-84`） |
| ffmpeg | ✅ 自带二进制 | `imageio-ffmpeg` 静态二进制，不是系统依赖（`media.py:63-70`） |
| Apple Speech | ❌ 仅 macOS | 依赖 Speech/AVFoundation + TCC 授权（`asr/apple_speech.py:63-70`） |
| Vision OCR | ❌ 仅 macOS | 依赖 Vision/Quartz，无替代实现（`ocr.py:15-23`） |

### 1.4 可复用的接口契约

| 端点 | 形态 | 说明 |
| --- | --- | --- |
| `POST /api/resolve` | 异步 202 + 轮询 | 只接受 `url`，服务端自行下载解析 —— **这正是"无浏览器输入通道"**（`service.py:146-160,663-665`） |
| `POST /api/transcribe` | 异步 202 + 轮询 | 支持 `media_url` 或 `data_base64`；**服务端本来就支持 multipart 上传**，只是扩展从未使用（`service.py:162-250,688-700`） |
| `POST /api/format` | **同步** | 纯文本整理，不跑 ASR（`service.py:455-486`） |
| `GET /api/jobs/{id}` | 同步 | `state/stage/progress/result/error`（`service.py:643-650`、`jobs.py:41-56`） |
| `GET /api/health` | 同步 | ffmpeg/ASR 引擎/缓存状态（`service.py:131-144`） |

关键契约细节：
- 无 `Origin` 的请求**被放行**（`service.py:489-493`，测试 `tests/test_service.py:127`）→ 服务端到服务端的调用天然可用；
- 扩展**从不发送** `X-LinkWork-Token`（`extension/` 内 grep `token` 零命中），而 token 仅在非空时校验（`service.py:113-117`）→ **给现有服务加必填 token 会直接打断浏览器插件**，这是"不改现有代码"的硬理由之一；
- `JobManager` **没有超时、没有取消**，上限 200 条、内存态（`jobs.py:23` 全文）→ 公网暴露前必须在外层补齐；
- ASR 子进程超时 600s（`asr/isolated.py:28`）→ 与扣子插件 600s 上限正好顶格，不能依赖单次同步调用。

### 1.5 环境变量（可用来做双形态隔离）

| 变量 | 作用 | 位置 |
| --- | --- | --- |
| `LINKWORK_CACHE_DIR` | 媒体/工作区根目录 | `cache.py:34` |
| `LINKWORK_OUTPUT_DIR` | 导出目录 | `pipeline.py:24` |
| `LINKWORK_FFMPEG` / `FFMPEG_BINARY` | ffmpeg 路径 | `media.py:51` |
| `LINKWORK_WHISPER_MODEL` | 默认模型 | `asr/faster_whisper_engine.py:18` |
| `LINKWORK_CACHE_DIR` 之外 | ⚠️ 服务日志路径 `~/Library/Logs/LinkWork-service.log` **写死、无环境变量** | `diagnostics.py:391` |

---

## 2. 扣子侧硬约束（决定方案形状）

| # | 约束 | 数值 / 事实 | 对 LinkWork 的影响 |
| --- | --- | --- | --- |
| 1 | 插件 URL 格式 | **必须是域名，不支持 IP** | 云侧插件必须有自己的 HTTPS 域名，`127.0.0.1:17890` 用不了 |
| 2 | 请求发起方 | Coze 云端服务器 | 摸不到用户本机内网/回环 |
| 3 | 接入协议 | 支持 OpenAPI 3.0 / Swagger 2.0 / Postman Collection 导入 | 我们要交付一份 `openapi.yaml` |
| 4 | **同一插件内所有工具必须同域名、同路径前缀** | 多 API 导入约束 | 所有工具统一挂在 `/coze/v1/*` 下，不能东一个西一个 |
| 5 | 插件节点超时 | 默认 **180s**，可调 0.1s ~ **600s** | ASR 常常超过 180s → 必须支持"提交 + 轮询"两段式 |
| 6 | 入参 / 输出上限 | 插件节点入参 **2MB**；节点输出 **10MB**；IDE 试运行 64KB | 几万字文案（几十 KB）安全；但**音频不能走 base64 入参**，必须用 URL 或文件参数 |
| 7 | 工作流总超时 | 同步 **10 分钟** / 异步运行 **24 小时** | 长音频必须走异步工作流 |
| 8 | 异步插件节点 | **不支持**。需"循环节点 + 代码节点 `asyncio.sleep`"自行轮询，`sleep` 单次最长 60s | 轮询循环是唯一合规做法 |
| 9 | 代码节点能力 | Python 3.11 标准库（仅 `requests_async`+`numpy`）/ JS Node 20（仅 `dayjs`+`lodash`）；**不能装第三方库**；**禁 `sockets`/`threading`/`multiprocessing`**；单请求 **60s** | **不能跑 ffmpeg、不能跑 whisper、不能跑真浏览器** → 重活必须留在 LinkWork |
| 10 | 端插件（客户端插件） | 请求由**用户自己的业务服务端**执行，官方示例即"读本地目录/截屏"，**可触达本机**；`requires_action` 可挂 **24h**；端插件节点**无超时限制** | 唯一官方正路让扣子摸到本机 LinkWork |
| 11 | 端插件的代价 | **仅支持 API 方式**（`v3/chat` → `requires_action` → `submit_tool_outputs`）、**页面内无法调试**、**大模型节点不支持配置端插件**（工作流中可用） | 意味着"用扣子当大脑的本地应用"，而不是"商店里能被别人添加的插件" |
| 12 | 私网模式插件 | 仅**企业旗舰版** + API 必须部署在**火山引擎华北 2** + **不支持上架** | 不做为默认路径 |
| 13 | 官方 ASR 插件 | 时长 **<30min**、文件 **<10MB**、并发 10 | 轻量音频场景可用现成的，省掉自建 whisper |
| 14 | 音频文件参数 | WAV/MP3/FLAC/M4A/AAC/OGG/WMA/MIDI；上传单文件 ≤512MB；**只给 `file_id`，官方明说"不支持下载"** | 要可下载 URL 得自己上对象存储 |
| 15 | 云端浏览器自动化 | **没有**。官方 Scraper 仅 Chrome 扩展、面向人工采集、不能在插件/工作流里调用 | 云侧**不可能**复刻抖音/小红书取数 |
| 16 | 自托管 | 官方"扣子编程暂不支持私有化部署"，指向开源 **Coze Studio**（Apache 2.0，2C4G，Docker Compose，默认 `localhost:8888`） | 自托管是可行第三条路 |
| 17 | 自托管访问宿主机 | 容器内 `127.0.0.1` ≠ 宿主机，需 `host.docker.internal`（Linux 需 `host-gateway`） | 能直连本机网关 |
| 18 | 自托管 SSRF | 仓库有公开 Issue #2711 / CVE-2026-92795（**修复状态未验证**） | 若走此路，必须先在目标版本实测 |
| 19 | CLI 能力 | `coze` 0.3.10 已装且已登录；`coze code project create --type` 支持 `workflow`，**不支持 `plugin`**；`coze plugin *` 是 CLI 自身开关，不是平台插件 | **扣子插件只能走 Web 控制台或 OpenAPI 导入**，CLI 帮不上；工作流可用 CLI 创建 |
| 20 | 官方未明确项 | 无"插件响应体大小上限"明文；无"工作流输出最大字符数"明文 | 设计上按 10MB 输出上限保守处理，并加截断参数 |

---

## 3. 四条路线对比与推荐（v2）

| 维度 | A. 云侧插件 | B. 端插件（纯本机服务端） | **D. 端插件 + Playwright 登录通道** | C. 自托管 Coze Studio |
| --- | --- | --- | --- | --- |
| 满足"抖音/小红书口播稿"核心需求 | ❌ **出局** | ❌ 仍拿不到（服务端无登录态） | ✅ **唯一可行** | ✅（配合 D 的通道） |
| 扣子页面里能编排/调试 | ✅ | ❌（仅 API） | ❌（仅 API） | ✅ |
| 保住 B站/YouTube/网页/直链 + Whisper | ✅ | ✅ | ✅ | ✅ |
| 保住 macOS Vision OCR / Apple Speech | ❌ | ✅ | ✅ | ✅ |
| 改动现有代码 | 不需要 | 不需要 | **不需要**（只读复用 `extractor.js`） | 不需要 |
| 需要公网 HTTPS 域名 | ✅ 必须 | ✅ 必须（Coze 回调你的服务端） | ✅ 必须（同上） | ❌ 不需要 |
| 需要本机常驻 | ❌ | ✅ | ✅ + 浏览器 profile | ✅ + Docker |
| 额外运维 | 云主机 + 域名 + 证书 | 内网穿透/隧道 | 隧道 + profile 维护 | 一整套中间件 |
| 落地成本 | 中 | 高（Coze 回调协议） | **高**（回调协议 + 浏览器通道） | 中高 |

### 推荐（按你的决策重排）

**主路线：D。** 理由：它是**唯一**同时满足"抖音/小红书口播稿"+"不改现有代码"的方案，而它成立的技术前提已经在本仓库里被验证过（`tests/extension_extractor.test.js` 在 Node 里跑 `pageExtractor()`）。

**备选与叠加：**

- **C（自托管 Coze Studio）**：若你不想暴露公网 / 不想做内网穿透，可把 D 的网关换成 Coze Studio 直连 `host.docker.internal:17891`。⚠️ 采用前须实测其 SSRF 修复状态（硬约束 #18，修复状态未验证）。
- **B 降级版**：若你愿意放弃抖音/小红书在扣子里的自动化，B 是成本最低的闭环（不需要浏览器通道）。
- **A（云侧插件）**：**降级为可选补充**。它仍有一个独立价值：让扣子能处理**公开平台**（B站/YouTube/公众号/直链音频），且能被别人添加。可作为 D 之外的第二个插件，但**不满足你的核心需求**。

### 一条零开发的替代路径（供对比）

**半自动交接**：继续用浏览器插件提取抖音口播稿 → 用户复制文案 → 粘进扣子工作流做后续加工（改写/翻译/分发）。
零开发、零风险，但扣子只是下游消费者，不是"能自己取数的插件"。若你只是想"在扣子里用上这些文案"，这条可能已经够用；若你要的是"扣子里自己产出"，就必须做 D。

---

## 4. 推荐架构（只扩展，不改现有代码）

```
LinkWork/
├── extension/                        # 【冻结】浏览器插件，一行不动
├── src/linkwork/                     # 【冻结】现有服务与能力库，一行不动
├── docs/coze/
│   ├── feasibility-report.md         # 平台侧调研（已产出）
│   └── implementation-plan.md        # 本文
└── coze/                             # 【新增】全部扩展代码都在这里
    ├── gateway/
    │   └── linkwork_coze/            # 新 Python 包：Coze 适配网关（主路线 D 的核心）
    │       ├── __init__.py
    │       ├── app.py                # 独立 HTTP 服务（新端口，默认 17891）
    │       ├── capabilities.py       # 部署位/平台/引擎能力探测与上报
    │       ├── tasks.py              # 在现有 JobManager 之上补：等待、超时、上限
    │       ├── guard.py              # 鉴权 + SSRF 白名单 + 频率/并发/体积限流
    │       ├── views.py              # /coze/v1/* 路由与出参裁剪
    │       ├── errors.py             # 面向扣子的错误码与可读提示
    │       └── browser/              # 【路线 D 新增】Playwright 登录通道
    │           ├── session.py        # 持久化 profile 管理 + 首次登录引导
    │           ├── driver.py         # 加载页面、等待媒体就绪
    │           ├── extractor_host.py # 只读注入 extension/extractor.js 并取回结果
    │           └── platform.py       # 平台判定与"是否需要浏览器通道"的路由决策
    ├── plugin/
    │   ├── openapi.yaml              # 扣子插件 OpenAPI 3.0 定义（统一 /coze/v1 前缀）
    │   ├── console-setup.md          # 控制台建插件的手工步骤（CLI 不支持）
    │   └── import-notes.md           # 同域名/同前缀等导入注意事项
    ├── client/                       # 【路线 D/B 必需】端插件回调客户端
    │   └── linkwork_coze_client/     # v3/chat → requires_action → 执行 → submit_tool_outputs
    ├── workflow/
    │   ├── design.md                 # 工作流节点图与参数
    │   └── workflow.export.json      # 可导入的工作流定义（若控制台支持导出/导入）
    ├── deploy/
    │   ├── Dockerfile                # 【仅路线 C/A 需要】
    │   ├── docker-compose.yml
    │   └── env.example
    └── tests/                        # 【新增】只加不改
        ├── test_gateway_api.py
        ├── test_guard_ssrf.py
        ├── test_browser_channel.py   # 用本地 HTML fixture 验证通道，不打真实平台
        └── test_openapi_contract.py
```

**关于 `extractor_host.py` 的红线**：它**只读**加载 `extension/extractor.js`（`require` 或读取源码后注入页面），**绝不修改该文件**，也不复制一份出来改。若将来 `extractor.js` 升级，网关自动跟着升级——这是选择"复用"而不是"移植"的核心理由。若某天必须给它打补丁，那也要以"新增覆盖层"的方式做，不是改原文件。

### 数据流

```
扣子（端插件，仅 API 方式）
      │  v3/chat → requires_action → submit_tool_outputs
      ▼
coze/client（端插件回调客户端，常驻本机）
      │  HTTP（回环）
      ▼
coze/gateway（新进程 :17891，自己的鉴权与限流）
      │
      ├─ 需要浏览器登录态？（抖音/小红书）
      │        │ 是
      │        ▼
      │   coze/gateway/browser（Playwright 持久 profile）
      │        │  加载真实页面 → 只读注入 extension/extractor.js
      │        │  → 取回带签名的媒体直链
      │        ▼
      │   （继续走下面的现有服务）
      │
      │  Python import，进程内直调
      ▼
src/linkwork（现有能力，未修改）
   pipeline.extract_url / service.LinkWorkService / transcribe / resolvers / asr / ocr
      │
      ▼
本机磁盘缓存（独立 LINKWORK_CACHE_DIR）→ 结果 → 网关裁剪 → 端插件客户端 → 扣子
```

**为什么必须是独立进程**：
1. 现有服务绑 `127.0.0.1` 且 Origin 白名单只认扩展与 localhost（`service.py:48-53,703-713`）——公网入口不能复用这套信任模型；
2. 现有服务**不能**加必填 token（会打断不发送 token 的扩展）；
3. 网关需要长轮询、限流、SSRF 防护、体积/时长上限——这些是**策略**，不属于现有服务的职责；
4. 独立进程 = 浏览器插件与扣子插件**物理隔离**，一个挂了不影响另一个。

---

## 5. 扣子插件工具设计（OpenAPI 契约）

统一前缀 `/coze/v1`（硬约束 #4）。全部工具走 **API Key（Header）** 鉴权。

| 工具 | 方法/路径 | 入参 | 出参 | 时间预算 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `get_capabilities` | `GET /coze/v1/capabilities` | 无 | `deployment`(local/selfhost/cloud)、`platforms[]`、`browser_channel{available,logged_in_platforms[]}`、`engines{asr,ocr}`、`limits`、`version` | <1s | **让工作流据此分支**，不做能力猜测。**必须如实上报浏览器通道是否已登录** |
| `extract_from_url` | `POST /coze/v1/extract` | `url`(必填)、`wait_seconds`(默认 120，上限 570)、`subtitles_only`、`language`、`prefer`(`auto`/`server`/`browser`) | `state`、`task_id`、`method`、`via`(`server`/`browser`)、`result{text,title,author,platform,method,char_count,warnings}` 或 `truncated`+`full_url` | ≤570s | **统一入口**：`prefer=auto` 时由网关按平台决定走服务端还是浏览器通道 |
| `login_browser` | `POST /coze/v1/browser/login` | `platform`(`douyin`/`xiaohongshu`) | `state`、`qr_or_url`（首次登录引导）、`expires_at` | ≤60s | **仅本机部署位**。首次扫码登录；已登录则直接返回状态 |
| `transcribe_media` | `POST /coze/v1/transcribe` | `media_url` 或 `file_url`（**不接受 base64**，见硬约束 #6）、`title`、`wait_seconds` | 同 `extract_from_url` | ≤570s | 音频/视频直链 → 口播稿；浏览器通道取到的签名直链也从这里进 |
| `get_task` | `GET /coze/v1/tasks/{task_id}` | 路径参数 | `state`/`stage`/`progress`/`result`/`error` | <1s | 轮询用；出参契约对齐现有 `jobs.py:41-56` |
| `format_text` | `POST /coze/v1/format` | `text`、`title`、`author`、`url` | 整理后文案 | <5s | 同步；复用 `service.py:455-486` |
| `ocr_images` | `POST /coze/v1/ocr` | `images[]` | 图片文字 | ≤300s | **仅本机部署位可用**（macOS Vision）；云端在 `capabilities` 里标 `unavailable` |

### 设计要点

- **不新增业务逻辑**：每个工具都是"参数翻译 + 调用现有函数 + 出参裁剪"，业务能力全在 `src/linkwork` 里。
- **大文本策略**：默认返回正文全文；提供 `max_chars`（默认 20000）；被截断时返回 `truncated: true` 与 `full_url`（落盘文件 + 一次性下载链接）。几万字（几十 KB）远低于 10MB 节点输出上限，正常不触发。
- **错误即报错，不降级猜测**：沿用现有约定（`errors.py:18-21` 的 `user_message()`），把 `code` 与中文提示一起返回，让工作流能给出可操作指引（如"抖音需在浏览器插件里提取"）。
- **不做的事**：不在网关里做任何平台解析、不做签名、不做浏览器模拟。

---

## 6. 扣子工作流编排设计

```
[开始] 输入：link_or_text(string)  ← 也可以是 file（音频/视频）
   │
   ▼
[插件] get_capabilities ──────────► 拿到 deployment / platforms / engines
   │
   ▼
[选择器] 输入类型？
   ├─ 纯文本 ─► [插件] format_text ─────────────────────────────┐
   │                                                            │
   ├─ 音频/视频文件 ─► [插件] transcribe_media                   │
   │                                                            │
   └─ URL ─► [选择器] 平台？                                     │
        ├─ B站 / YouTube / 网页 / 直链 ─► [插件] extract_from_url │
        │        │                                               │
        │        └─ 若 state=running：                            │
        │           [循环节点] 最多 N 次（N≈20，覆盖 10 分钟）      │
        │              ├─ [代码节点] asyncio.sleep(3)  ← 硬约束 #8 │
        │              ├─ [插件] get_task                         │
        │              └─ [条件] state∈{done,error} → 退出循环     │
        │                                                        │
        └─ 抖音 / 小红书 ─► [插件] extract_from_url(prefer=auto)    │
                 │  网关内部走浏览器通道（Playwright 持久 profile）│
                 │  取回签名直链 → 现有服务下载 + ASR → 口播稿     │
                 │                                               │
                 └─ 若 capabilities 报 browser_channel 未登录：    │
                    [插件] login_browser → 返回扫码引导           │
                    [提示节点] "首次使用需在本机完成一次扫码登录"  │
   │                                                            │
   ▼                                                            ▼
[结束] 输出：text / title / author / platform / method / char_count / warnings
```

工作流层的关键约束对应：
- 插件节点超时设 **≤600s**（硬约束 #5），网关侧 `wait_seconds` 上限设 570s 留余量；
- 超过同步工作流 10 分钟的场景（长音频）→ 用**异步工作流运行**（24h，硬约束 #7）；
- 轮询只能靠**循环节点 + 代码节点 `asyncio.sleep`**（不支持异步插件节点，硬约束 #8），`sleep` 单次 ≤60s；
- 代码节点只做 `sleep` 与状态判断，**不碰网络、不碰文件**（硬约束 #9）。

---

## 7. 兼容性设计（浏览器插件 + 扣子插件并存）

### 7.1 天然兼容的部分

| 项 | 结论 |
| --- | --- |
| 现有服务与扩展 | **零改动**（网关是另一个进程、另一个端口），扩展所有请求路径、字段、超时、轮询策略全部不变 |
| 现有测试基线 | 现有 94 个测试（`tests/test_service.py` 30 + `tests/test_core.py` 64）**必须全绿**，作为"没改坏"的证明 |
| OpenAPI 契约 | 两种部署位共用同一份 `openapi.yaml`，避免两套定义漂移 |

### 7.2 需要显式处理的冲突

| 冲突点 | 风险 | 处置 |
| --- | --- | --- |
| 缓存目录 | 两个进程共用 `~/Library/Caches/LinkWork`，两边启动都会 `purge_orphans()`（`cache.py:137-157`），理论上可能互删工作区 | 网关用**独立** `LINKWORK_CACHE_DIR`（如 `.../LinkWork/coze`），并在计划验收里验证 |
| 内存/CPU | 两个进程各自加载 Whisper 模型（small ≈ 数百 MB） | 网关加**并发上限**（建议默认 1~2）；文档写明同时使用时的内存占用 |
| 任务表 | 两边各有独立内存任务表，`task_id` 不互通 | 接受；网关的 `task_id` 只对网关有效，出参里不暴露现有服务的 job id 语义 |
| 端口 | 17890 已被现有服务占用 | 网关默认 **17891**，可配置 |
| 版本号 | 仓库规则要求"每改一次代码递增版本号"，而 `scripts/bump_version.py` 会**改 5 个现有文件**（`VERSION`、`pyproject.toml`、`src/linkwork/__init__.py`、`extension/manifest.json`、`extension/extractor.js`），且 `tests/test_core.py::test_version_is_consistent_everywhere` 强制一致 | **决策点 D3**。建议：`coze/` 自带独立版本号，只在正式发布扣子插件时按仓库规则 bump，并把 bump 作为独立可回滚的一步 |

### 7.3 明确「绝不动」的清单

- `src/linkwork/**`（含 `resolvers/**`、`asr/**`）——一行不改；
- `extension/**`——一行不改，**不删**侧边栏、`extractor.js`、`background.js`；
- `scripts/**`、`tests/**` 下的现有文件——只新增，不修改；
- 不给现有服务加必填 token、不改其 Origin 白名单、不改其绑定地址。

若某一步"非改不可"（例如必须在现有服务里加一个端点才能完成），**先停下来说明**，按"最小且向后兼容、由环境变量/开关控制、默认行为不变"的方式另立一步，并在本文件记录理由。

---

## 8. 分阶段里程碑

### Phase 0 — 前置（最小化，按你的决定不做 git init）

| 产出 | 说明 |
| --- | --- |
| ~~`git init`~~ | **已决定不执行**（D4）。Phase 1 为纯新增目录，删目录即回滚，可以接受；但见 §0.4 准入门槛 |
| 基线测试记录 | 跑一次 `tests/`（含 JS 测试），把结果存档，作为"改动前"对照。这是"没改坏"的唯一证据来源 |
| 运行态校准 | 当前**运行中的服务是 0.2.13，磁盘代码是 0.2.14**（`/api/health` vs `VERSION`）；验证前必须重启服务，否则测的是旧代码 |

**验收**：测试结果存档；`/api/health` 版本与 `VERSION` 一致；确认 `coze/` 目录尚不存在。

### Phase 1 — 本机适配网关骨架（不含浏览器通道）

产出：`coze/gateway/linkwork_coze/**`、`coze/tests/**`

- 独立 HTTP 服务（默认 `127.0.0.1:17891`，仅回环）；
- 实现除浏览器通道外的工具（§5 表）：`get_capabilities`、`extract_from_url`（`prefer=server`）、`transcribe_media`、`get_task`、`format_text`、`ocr_images`；`get_capabilities` 中 `browser_channel.available = false`；
- `tasks.py`：在现有 `JobManager` 之上补"等待到完成/超时/取消"逻辑（现有 `jobs.py` **无超时无取消**，见 §1.4）；
- `guard.py`：鉴权 + 出参裁剪 + 频率/并发/体积限流（Phase 1 仅回环，SSRF 防护按"提前就位"实现，Phase 5 才真正承压）；
- 出参裁剪，对齐现有 `_dump_result` 的字段语义（`service.py:85-101`）。

**验收**：新增测试覆盖各工具的成功/失败/超时/超限；**现有 94 个测试全绿**；通过网关提取一条 B站视频 + 一篇公众号文章成功；浏览器插件端到端跑通一次提取，行为与改动前一致。

### Phase 2 — 浏览器通道（路线 D 的关键，打通抖音/小红书）

产出：`coze/gateway/linkwork_coze/browser/**`、`coze/tests/test_browser_channel.py`

- Playwright 持久化 profile 管理：首次 `login_browser` 返回扫码引导，之后 cookie 长期复用；
- `extractor_host.py`：**只读**加载 `extension/extractor.js`，注入页面主世界并取回结构化结果（复用其 `parseAssignedJson`/`parseRenderData`/`scanForMedia`/`isRealMedia`/候选排序）；
- `platform.py`：平台判定 + `prefer=auto` 的路由决策；
- 拿到的签名媒体直链交给现有 `transcribe_media` 链路 → 口播稿。

**验收**：
- 用**本地 HTML fixture** 单元测试通道逻辑（不打真实平台，避免把风控/网络问题混进功能测试）；
- 真实链路上，抖音详情页与小红书视频笔记各取到带签名的媒体直链并产出**口播稿**；
- 确认 `extension/extractor.js` 与 `src/linkwork/**` 的 **diff 为空**；
- 确认浏览器插件在网关运行时仍能正常工作（双形态共存）。

### Phase 3 — 端插件回调客户端

产出：`coze/client/linkwork_coze_client/**`

- 实现 Coze 端插件协议：`v3/chat` → 检测 `requires_action` → 本地执行（调网关） → `submit_tool_outputs`（硬约束 #10/#11）；
- 处理 `requires_action` 挂 24h 的语义：客户端可重启后继续（需持久化未完成的任务映射）；
- 在扣子控制台创建**端插件**并绑定（控制台操作，CLI 不支持创建插件，硬约束 #19）。

**验收**：通过 API 调用扣子 Bot → 端插件被触发 → 本机完成一次抖音口播稿提取 → 结果回到对话；杀掉客户端再重启，未完成的 `requires_action` 不丢。

### Phase 4 — 扣子工作流编排（API 方式）

产出：`coze/workflow/design.md`、`coze/workflow/workflow.export.json`

- 按 §6 节点图搭建；轮询循环 + `asyncio.sleep` 代码节点；
- 登录引导分支：`capabilities` 报未登录 → `login_browser` → 提示用户扫码后重试；
- ⚠️ 端插件**页面内无法调试**（硬约束 #11），编排与排错必须走 API；工作流本身可用 `coze code project create --type workflow` 创建（CLI 支持）。

**验收**：四类输入（B站链接 / 抖音短链 / 音频文件 / 纯文本）端到端出文案；故意造一次未登录与一次超时，确认提示可读且不误导。

### Phase 5 —〔可选〕替代部署位

两条互斥的替代方案（按需选一，不必都做）：

- **自托管 Coze Studio**：Docker Compose 起 Coze Studio，插件指向 `host.docker.internal:17891`；优点是不必做内网穿透、可在页面里编排。⚠️ **先实测其 SSRF 修复状态**（硬约束 #18，修复状态未验证）再决定。
- **云侧插件（仅公开平台）**：把网关的 `prefer=server` 子集部署到公网 HTTPS 域名，交付 `coze/plugin/openapi.yaml` 供控制台导入，让扣子能处理 B站/YouTube/公众号/直链音频。**不满足抖音/小红书需求**，仅作为"给别人用"的补充形态。

**验收（自托管）**：Studio 内的工作流成功调用本机网关产出抖音口播稿。
**验收（云侧）**：控制台 6 个工具调试通过；`get_capabilities` 如实返回 `deployment: cloud`、`browser_channel.available: false`、`engines.ocr: unavailable`。

---

## 9. 风险清单

| 级别 | 风险 | 缓解 |
| --- | --- | --- |
| 🔴 高 | **平台风控**：浏览器通道仍是自动化访问，抖音/小红书可能识别并拦截 | 用真实浏览器 + 真实登录态 + 真实页面（**不逆向签名**）；限频、单账号、不做批量；失败时如实报错并提示改用浏览器插件手动提取 |
| 🔴 高 | **登录 profile 失效/过期**：cookie 过期或被踢，抖音/小红书取数突然全挂 | `get_capabilities` 上报 `logged_in_platforms`；工作流有 `login_browser` 引导分支；状态探测 + 明确提示，**不静默降级成"无文案"** |
| 🔴 高 | **鉴权缺失**：现有服务默认无 token，扩展也不发 token | 网关独立鉴权（API Key / 端插件回调令牌），与现有服务策略解耦；**不给现有服务加必填 token** |
| 🔴 高 | **端插件暴露面**：这是本机服务对外开放的入口 | 仅回环 + 端插件客户端主动外连（不监听公网）；令牌校验；来源与频率限制 |
| 🟠 中 | **SSRF / 开放代理**：`transcribe_media` 接受任意 `media_url` | 网关层强制 URL 白名单（协议/私网/回环/云元数据地址拦截）、禁重定向跟随、大小与时长上限 |
| 🟠 中 | **任务超时**：ASR 子进程上限 600s；端插件节点无超时但工作流同步上限 10min | 网关 `wait_seconds` ≤570s；长音频走异步工作流（24h）；`requires_action` 任务映射持久化 |
| 🟠 中 | **资源耗尽**：Whisper CPU + 内存 + Playwright 浏览器常驻；现有 `MAX_JOBS=200` 内存任务表无超时无取消 | 网关并发上限（含浏览器上下文上限）、任务 TTL 与自动清理、浏览器上下文复用而非每次新建 |
| 🟠 中 | **平台改版**：解析逻辑依赖页面结构 | 复用 `extension/extractor.js`（改版时现有插件与网关**同时**受益，只需维护一处）；复用现有 `warnings` 与诊断日志 |
| 🟠 中 | **缓存互删**：两进程共用缓存根目录（两边启动都会 `purge_orphans()`） | 网关独立 `LINKWORK_CACHE_DIR`，并在验收里验证 |
| 🟡 低 | **无回滚网**：已决定暂不做 `git init` | Phase 1 纯新增（删目录即回滚）；**任何要动现有文件的步骤前必须先建安全网**（§0.4 准入门槛） |
| 🟡 低 | **合规与滥用** | 频控 + README 已有免责声明；明确不做验证码绕过、不做批量抓取、不做签名逆向；仅个人学习与内容整理用途 |
| 🟡 低 | **自托管 SSRF** | 采用前实测（硬约束 #18）；不默认推荐 |

---

## 10. 总验收标准

1. **现有形态零回归**：浏览器插件端到端功能与改动前完全一致；现有 94 个测试全绿；`extension/**` 与 `src/linkwork/**` 的 diff **完全为空**（本方案不需要任何版本号 bump 之外的改动，因为 `coze/` 自带独立版本号）。
2. **核心需求达成**：在扣子里（经端插件，API 方式）输入一条**抖音详情页链接**，产出**口播稿**（不只是分享页文案）；小红书视频笔记同样跑通一次。
3. **零改动被验证**：`extension/extractor.js` 被网关只读复用，且其内容哈希与改动前一致。
4. **双形态共存**：网关运行时，浏览器插件同时可用；两边的缓存目录互相隔离，不互删。
5. **能力诚实**：`get_capabilities` 与实际能力一致（含浏览器通道登录状态）；未登录/过期时给出可操作引导，而非静默返回空文案或降级结果。
6. **安全**：无令牌不可调用；内网/元数据地址被拒；并发与频控生效。
7. **可回滚**：每完成一个 Phase 都能回到上一个里程碑（Phase 1 起为"删目录即回滚"；触碰现有文件前另建安全网）。

---

## 11. 待决策问题

### 已决议

| # | 问题 | 结论 |
| --- | --- | --- |
| D1/D2 | 抖音/小红书是否核心场景 → 决定路线 | **是核心，必须口播稿** → 云侧插件出局，主路线 = **D（端插件 + Playwright 登录通道）** |
| D3 | `coze/` 是否跟随仓库版本号体系 | **自带独立版本号**，仅正式发布时按仓库规则 bump |
| D4 | 是否立刻执行 `git init` | **暂不执行**；改为依赖"纯新增 = 删目录即回滚"，并在 §0.4 设准入门槛 |
| D5 | 下一步 | **Phase 0 + Phase 1**（本地适配网关骨架） |

### 仍需你确认

| # | 问题 | 为什么必须先定 |
| --- | --- | --- |
| **D6** | 接受 **路线 D 的全部代价**吗？具体是：① 需在本机首次扫码登录一个**独立**浏览器 profile（与日常浏览器分离）；② Mac 需常驻（开机 + 网关/客户端进程）；③ 端插件**页面内无法调试**，只能走扣子 API；④ Playwright + 浏览器运行时体积。 | D6 是路线 D 能否成立的唯一前提。若其中任一条不可接受，则退化为"B 降级版（扣子在扣子里只做公开平台）"或"半自动交接（浏览器插件提取 → 粘贴到扣子）" |
| **D7** | 浏览器通道要不要**复用系统已有的 Edge/Chrome 可执行文件**（省下 ~150MB 下载）？还是用 Playwright 自带 Chromium？ | 影响部署体积与兼容性；复用系统浏览器需处理版本漂移 |
| **D8** | 登录 profile 的存放位置与加密要求？（默认建议放在 `~/Library/Application Support/LinkWork/coze-profile`，与 `LINKWORK_CACHE_DIR` 分开） | profile 内含登录凭证，属敏感数据，需要你确认存放策略与是否加密 |
| **D9** | 扣子侧的凭证你有吗？端插件配置需要**扣子 API Token** + 一个 Bot/工作流 ID。 | Phase 3 起必需；没有的话 Phase 1/2 仍可先做（纯本地） |

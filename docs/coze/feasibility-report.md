# 扣子（Coze）插件 + 工作流 技术机制与硬性限制调研报告

> **调研目的**：判断能否把 LinkWork（浏览器插件 + 本地 Python 服务）改造/扩展为扣子插件。
> **调研方式**：仅联网研究 + 阅读本机 Coze CLI 文档。未修改任何代码、未创建 Coze 项目、未部署任何服务。
> **调研日期**：本机 Coze CLI v0.3.10 环境
> **重要说明**：所有数字均来自官方文档原文。凡官方文档未明确者，一律标注「未能验证」，不做乐观推测。

---

## 目录

1. [插件接入方式：公网 HTTPS / OpenAPI 导入 / 超时 / 响应体上限](#1-插件接入方式公网-https--openapi-导入--超时--响应体上限)
2. [插件能否访问 127.0.0.1 / 内网；云版请求从哪里发出](#2-插件能否访问-127001--内网云版请求从哪里发出)
3. [工作流节点能力：超时 / 异步轮询 / 代码节点边界](#3-工作流节点能力超时--异步轮询--代码节点边界)
4. [工作流输入输出类型：文件音频 / 大文本](#4-工作流输入输出类型文件音频--大文本)
5. [私有部署 / 自托管与宿主机 127.0.0.1 可达性](#5-私有部署--自托管与宿主机-127001-可达性)
6. [官方浏览器自动化 / 网页抓取能力与登录态数据](#6-官方浏览器自动化--网页抓取能力与登录态数据)
7. [本机 Coze CLI 现状与 `--type` 支持](#7-本机-coze-cli-现状与---type-支持)
8. [硬约束清单](#硬约束清单)

---

## 1. 插件接入方式：公网 HTTPS / OpenAPI 导入 / 超时 / 响应体上限

### 结论

| 问题 | 结论 |
|---|---|
| 是否必须公网可访问？ | **云侧插件必须以域名形式提供公网可达的 API**。官方明确「插件 URL 必须为域名格式，暂不支持 IP 格式的 URL 地址」。云端请求由 Coze 服务器发起（见第 2 节）。 |
| 是否支持 OpenAPI 3.0 导入？ | **支持**。官方明确支持 **OpenAPI、Swagger（OpenAPI 2.0）、Postman Collection** 三种协议，且文档给出的示例就是 `openapi: 3.0.1`。 |
| 插件调用超时上限 | **插件节点默认 180s（3 分钟），可调范围 0.1s ~ 600s（10 分钟）**。这是官方插件节点页面给出的原文区间。 |
| 响应体大小上限 | **未能验证（官方文档没有给出「插件响应体大小上限」这个数字）**。但有明确的替代性硬约束：插件节点入参上限 **2MB**；插件节点输出属工作流节点输出，上限 **10MB**；IDE 插件试运行输出上限 **64KB**。 |

### 证据

**（a）插件 URL 必须是域名，不支持 IP**

> 插件 URL | 插件的访问地址或相关资源的链接，例如 `https://www.example.com/api`
> 说明：**插件 URL 必须为域名格式，暂不支持 IP 格式的 URL 地址。**
> —— https://docs.coze.cn/guides_services （「基于 API 创建插件」→ 步骤一 → 设置基础信息）

导入插件页面重复了同一约束：

> 插件 URL ... 不可修改，示例值 `https://www.example.com/api` ...
> 说明：**插件 URL 必须为域名格式，暂不支持 IP 格式的 URL 地址。**
> —— https://docs.coze.cn/guides_import （「通过 JSON 或 YAML 文件导入插件」）

同一页面还指出：**基于已有服务创建的云侧插件，「私网连接」只支持选择「不使用私网连接」**（私网走单独的私网模式插件，仅企业旗舰版，见下文）。

> 私网连接 | 基于已有服务创建云侧插件时，只支持选择**不使用私网连接**。
> —— https://docs.coze.cn/guides_services

**（b）OpenAPI 3.0 导入确实支持**

> 定义 API 的 JSON 或 YAML 文件仅支持 **OpenAPI**、**Swagger**（2.0）或 **Postman Collection** 协议
> —— https://docs.coze.cn/guides_import

文档给出的 OpenAPI 示例头部即：

```YAML
openapi: 3.0.1
servers:
    - url: https://httpbin.org
```
—— https://docs.coze.cn/guides_import （「JSON 或 YAML 文件示例 → OpenAPI 3.0」）

导入时的额外硬约束：

> 如果您需要单次导入多个 API，则需要确保各个 API 有**相同的 URL 路径前缀**，该路径前缀将会作为插件 URL 来使用。如果单次导入的 API URL 路径前缀不一致，则会导入失败。
> —— https://docs.coze.cn/guides_import

**（c）插件调用超时：3 分钟默认，10 分钟上限**

官方「插件节点」页面（异常处理设置）：

> **整体执行超时**：整体执行超时指插件节点运行的最大耗时，如果超过此时长，则判断为节点运行超时。
> 默认情况下，插件节点的超时时间为 **180s，即 3 分钟**。你也可以将其改为 **0.1s~600s**，灵活控制超时时间。
> —— https://docs.coze.cn/guides_plugin_node

官方「低代码工作流使用限制」页面的超时总表：

> | 场景 | 模型节点、**插件节点**、语音播客 | HTTP 节点 | 视频生成 | 图像生成节点 | 数据库节点、意图识别节点、**代码节点**、画板节点、视频抽帧 | 其他节点 | 低代码**工作流整体** |
> | 默认值 | **3 分钟** | 2 分钟 | 6 分钟 | 10 分钟 | 1 分钟 | 无单节点限制 | 同步运行：10 分钟 / 异步运行：24 小时 |
> | 最大值 | **10 分钟** | 10 分钟 | 10 分钟 | 10 分钟 | 1 分钟 | 无单节点限制 | 同上 |
> —— https://docs.coze.cn/guides_workflow_limits

同一页面另有一条对 LinkWork 极其关键的例外：

> **端插件类型的插件节点无超时时间限制。**
> —— https://docs.coze.cn/guides_workflow_limits

**（d）响应体大小：官方未给数字，只有间接约束**

> | 请求大小 | 低代码工作流支持的请求大小上限为 **20MB**，包括输入参数以及运行期间产生的消息历史等所有相关数据。 |
> | 节点入参 | 运行低代码工作流时，工作流节点的输入、输出的上限均为 **10MB**；**代码节点、插件节点入参上限为 2MB**，超出限制会报错 `Invalid Request: input size exceeded`，或使用省略号截断。试运行 IDE 插件节点时，其输出上限为 **64KB**。 |
> —— https://docs.coze.cn/guides_workflow_limits

> **未能验证**：官方文档中不存在一条「插件响应体（response body）最大 X MB」的明文规定。网络上流传的「1MB / 200KB」等数字，我未能找到官方出处，因此不采信。

**（e）其他插件侧硬限**

> | 插件数量 | 每个工作空间下最多可创建 **1000 个插件**；每个账号下最多可创建 **30 个 IDE 插件**。 |
> | 插件工具数量 | 每个插件中最多包含 **100 个工具**。 |
> | QPS | 自定义插件的 QPS 最大值为 **50**。 |
> | 依赖包总大小 | 开发插件时，支持添加的依赖包总大小上限为 **250 MB**。 |
> —— https://docs.coze.cn/guides_plugin

> **推论（需明确标注为我的推论，非官方原文）**：同一个插件内不同工具**必须使用相同域名**。
> 原文：「每个插件可添加多个工具，**同一个插件内的不同工具必须使用相同的域名**。」
> —— https://docs.coze.cn/guides_plugin

### 对 LinkWork 的影响

- LinkWork 的本地 Python 服务监听 `127.0.0.1`，**无法直接作为云侧插件的 API 地址**：既因为插件 URL 不支持 IP 格式，也因为 `127.0.0.1` 从 Coze 云端无意义。
- 改造成本最低的路径是：**把本地服务的 API 暴露到公网**（HTTPS 域名 + OpenAPI 3.0 schema）。若 LinkWork 的 Python 服务已有清晰的 REST 接口，OpenAPI 3.0 导入是可行的。
- 但**接口风格需要收敛**：「同一插件内不同工具必须同域名」「多 API 导入需相同路径前缀」意味着 LinkWork 需要整理成**单域名 + 统一前缀**的 REST 形态。
- 若 LinkWork 的能力依赖**长耗时任务**（如音频转写、批量抓取），3 分钟默认 / 10 分钟上限的插件超时是硬天花板；必须改造成「提交 → 立刻返回 task_id → 由调用方轮询」的两段式（见第 3 节）。
- 「端插件」这条线**不走公网**，是本报告中唯一能让 Coze 意图触达「用户本机」的官方机制，但限制极重（仅 API 调用），见第 2、5 节。

---

## 2. 插件能否访问 127.0.0.1 / 内网；云版请求从哪里发出

### 结论

- **官方云版（coze.cn / coze.com）的普通云侧插件，请求由 Coze 云端服务器发出**，不是用户端。**不能访问用户本机的 `127.0.0.1` / `localhost` / 内网地址。**
- 云侧插件**不能**访问私网，除非使用**私网模式插件**（走火山引擎私网连接 VPC 终端节点）——而它的限制是：**仅企业旗舰版 / 原企业版**、**API 服务必须部署在火山引擎华北 2 地域**、**不支持发布到模板和插件商店**。
- 官方存在一个**「端插件」（客户端插件）**机制：请求由**用户自己的业务服务端**执行，理论上可触达本机/内网。但**仅支持通过 API 方式使用**（`v3/chat` 的 `requires_action` + `submit_tool_outputs` 流程），**不支持在扣子页面里调试**，也**暂不支持在大模型节点中配置端插件技能**。这是「让 Coze 摸到用户本机」的唯一官方正路，但代价是必须自建对话编排。

### 证据

**（a）云侧插件请求来自 Coze 服务器（固定 IP 白名单功能直接证明这一点）**

> 扣子编程**默认使用动态 IP 调用插件**，也支持通过固定 IP 访问插件对应的服务...
> 开启固定 IP 调用功能后，扣子编程将通过固定 IP 地址列表中的 IP 访问插件对应的服务，**你可以将 IP 地址列表添加到防火墙白名单中**，实现对公网访问的精准控制。
> —— https://docs.coze.cn/guides_fixed_ip_access_plugin

请求由 Coze 服务端发出（因此可以从 Coze 侧固定出口 IP），这是「云端发出」的直接证据。

**（b）私网模式插件：唯一的云侧触达私网方式，门槛极高**

> 私网模式插件是一种安全接入方案，旨在帮助企业将部署在**私有网络（VPC）内的内部服务**（如 ERP、CRM、敏感数据处理系统等）封装为扣子编程插件工具。通过**火山引擎的私网连接技术**，可以实现单点单向通信，确保插件的服务地址不暴露至公网...
>
> | 限制项 | 说明 |
> | 套餐版本 | **企业旗舰版、原企业版**支持私网模式插件。 |
> | 地域要求 | 作为插件的 API 服务需要部署在火山引擎**华北 2** 地域，其他地域暂不支持与扣子编程进行私网连接。 |
> | 跨云/跨地域 | 如果你的业务部署在火山引擎其他地域或其他云厂商，你需要通过 **VPN 或专线**自行实现 VPC 互通。 |
> | 发布限制 | 私网模式插件**不支持发布到模板和插件商店**。 |
>
> 私网模式插件**只支持基于已有服务创建云侧插件**，不支持其他创建方式。
> 作为插件的 API 服务需要部署在火山引擎**华北 2** 地域。
> —— https://docs.coze.cn/guides_private_plugin

**（c）端插件（客户端插件）：请求由用户自己的业务服务端执行**

> **端插件**是扣子编程插件的一种类型，能够使低代码智能体**直接与本地设备进行交互**，实现对本地设备的控制和信息获取。
>
> 业务流程说明：
> 2. 请求转发：**设备将请求转发给业务服务端**。
> 3. 对话建立：**业务服务端通过 `v3/chat` 接口与扣子编程建立对话**。
> 5. 请求执行端插件：若需调用端插件，则**向业务服务端请求执行端插件**，此时对话状态流转至 `requires_action` 状态。
> 6. **执行端插件：业务服务端根据下发的端插件指令，向设备执行相应的业务逻辑，具体实现由业务端自己控制。**
> 7. 返回结果：**业务服务端通过扣子编程的 `submit_tool_outputs` 接口**，将端插件执行结果返回给扣子编程。
> —— https://docs.coze.cn/guides_local_plugin_overview

端插件的限制说明（原文）：

> - **使用方式：仅支持通过 API 方式使用端插件。**智能体绑定端插件后，调用发起对话接口与智能体对话时才能正常使用端插件能力。
> - **节点限制：工作流中支持添加端插件节点，但暂不支持在大模型节点中配置端插件技能。**
> - **调试方式：创建端插件时暂不支持在扣子编程中调试**，建议先在本地设备中调试完成后再创建并使用端插件。
> —— https://docs.coze.cn/guides_local_plugin_overview

端插件创建文档里，官方给的示例场景**就是读取本机文件与截屏**，佐证其「可触达本机」：

> 以"控制电脑完成简单任务"为例，端插件需要实现以下两个功能：
> - 获取电脑电量：通过截图和图片理解技术，获取当前电脑的电量状态。
> - **总结文件内容：读取本地目录中的文件，并提取其主要内容。**
>
> | 截图 | 直接对电脑屏幕进行全屏截图 | 无 | 图片 |
> | **列出目录下文件** | 列出指定目录下的所有文件 | 目录地址（字符串） | 文件列表（数组） |
> | **读取文件内容** | 读取指定文件的内容并返回 | 文件路径（字符串） | 文件内容（字符串） |
> —— https://docs.coze.cn/guides_create_local_plugin

`requires_action` 状态是长时挂起的，这一点对 LinkWork 有利：

> `requires_action` | 端插件场景中，调用端插件时，对话转为 `requires_action` 状态。业务服务端提交执行结果后，对话重新转为 `in_progress` 状态；**24 小时未成功提交执行结果转为 `expired` 状态**。
> —— https://docs.coze.cn/guides_local_plugin_overview

**（d）官方是否明文「禁止 localhost」**

> **未能验证**：官方文档中没有一句「禁止 `127.0.0.1`」的明文 SSRF 声明。第 1 节引用的「插件 URL 必须为域名格式，暂不支持 IP 格式的 URL 地址」是**格式层面**的拦截，实际上已足以让 `http://127.0.0.1:8000` 无法通过校验；且从云端视角 `127.0.0.1` 指向 Coze 自身，语义上也无效。

### 对 LinkWork 的影响

这是本报告对 LinkWork 最关键的一节，有**两条互斥的路线**：

**路线 A：云侧插件（把 LinkWork 的本地服务公开到公网）**
- 需要在公网部署 HTTPS 域名 + TLS 证书 + 鉴权。
- 架构从「本机 Python 服务」变成「云端/托管 Python 服务」，**这就不是 LinkWork 现在的形态了**。
- 优点：可在扣子低代码页面里直接编排、调试、发布；工作流插件节点可用；3~10 分钟超时可控。
- 缺点：失去了「浏览器插件 + 本地服务」的隐私/登录态优势。

**路线 B：端插件（保住本地服务，让 Coze 反向调用）**
- LinkWork 的本地 Python 服务**几乎不用改核心能力**：它只需要实现「向 Coze 发起 `v3/chat`」+「接收 `requires_action`」+「执行本地能力（浏览器插件已是本地！」）」+「回 `submit_tool_outputs`」。
- **但代价是**：端插件**只能在 API 调用链里生效**，在扣子网页版的对话里无法调试，也不能挂在大模型节点上做技能。也就是说，LinkWork 必须自己实现对话编排（自己调 `v3/chat`），Coze 变成了「推理大脑」，而 LinkWork 变成了「宿主」。
- 好处：端插件节点**没有超时限制**（第 1 节引文），`requires_action` 可挂 24 小时。
- **判断**：如果 LinkWork 的定位本身就是「用户的浏览器插件 + 本地服务」，那么**路线 B 在架构上远比路线 A 契合**；但因为「仅支持 API 方式使用」，它意味着 LinkWork 不会变成一个「在扣子商店里被大家添加的插件」，而是一个「用扣子当大脑的本地应用」。

---

## 3. 工作流节点能力：超时 / 异步轮询 / 代码节点边界

### 3.1 单节点超时与工作流总超时 —— 结论

| 项目 | 数值（官方） |
|---|---|
| 模型节点 / 插件节点 / 语音播客 | 默认 **3 分钟**，最大 **10 分钟** |
| HTTP 节点 | 默认 **2 分钟**，最大 **10 分钟** |
| 视频生成节点 | 默认 **6 分钟**，最大 **10 分钟** |
| 图像生成节点 | 默认 **10 分钟**，最大 **10 分钟** |
| 数据库 / 意图识别 / **代码节点** / 画板 / 视频抽帧 | 默认 **1 分钟**，最大 **1 分钟**（不可调高） |
| 其他节点 | 无单节点限制 |
| 工作流整体（同步运行） | **10 分钟** |
| 工作流整体（异步运行） | **24 小时** |
| 端插件类型的插件节点 | **无超时时间限制** |

> 在低代码工作流中可以通过**异步任务及异步触发节点**实现低代码工作流的异步运行
> **未开启异步运行时**，为确保低代码工作流运行的准确性和稳定性，建议将低代码工作流的执行时间控制在 **5 分钟**以内。
> 一次对话中，智能体连续两次回复的时间间隔限制为 **10 分钟**。
> —— https://docs.coze.cn/guides_workflow_limits

其他量化限制：

> - 每个低代码工作流中最多添加 **1000 个节点**。每次运行低代码工作流时，包含循环节点的循环次数、子工作流的运行节点在内，每次最多执行 **1000 个节点**。
> - 每个低代码工作流中最多添加 **50 个代码节点**。
> - **代码节点的并发限制为每个工作空间内 500**；单个节点维度 QPS：数据库相关节点 400，其它节点 3000。
> - 单个工作流维度 QPS：个人免费版 200，其余套餐 500。
> —— https://docs.coze.cn/guides_workflow_limits

### 3.2 是否支持异步任务 + 轮询范式 —— 结论

**支持，而且是官方明确推荐的范式，但形态与「插件内部长轮询」不同。**

有两种不同的「异步」，必须区分清楚：

1. **工作流级异步任务（官方一等公民）**：把业务工作流注册为「异步任务」，再用含**异步任务节点**的工作流去触发它。触发即返回，业务工作流在后台异步跑，整体超时 24 小时。
2. **插件级长任务轮询（需要自己手搭）**：官方在代码节点文档里给出了明确的做法。

证据（工作流级异步）：

> 扣子编程的低代码工作流**默认采用同步执行模式，通过创建异步任务可以实现工作流的异步执行**。
> 每次运行异步任务节点时，系统会**立即触发一次异步任务。任务触发成功即表示该节点运行完成**，可继续执行下一次触发，**无需等待业务工作流执行完成**。
> 说明：异步执行时，工作流整体超时时间为 **24 小时**，模型节点等部分节点为 10 分钟，数据库节点等部分节点为 1 分钟。
> —— https://docs.coze.cn/guides_execute_workflow_asynchronously

证据（插件级轮询范式，官方原文明说「工作流暂不支持异步插件节点」，改用循环 + `asyncio.sleep`）：

> 目前**工作流暂不支持异步插件节点**，如果下游节点需要使用异步插件的执行结果，**可以通过循环节点实现异步等待和结果轮询**，其中异步等待由代码节点的 `asyncio.sleep` 函数实现。
> `asyncio.sleep` 函数在异步编程中挂起当前任务、实现非阻塞的延迟，**最长可延迟 1 分钟**。
> —— https://docs.coze.cn/guides_code_node （「代码节点中如何使用 sleep 函数？」）

> **关键推论（标注为推论）**：即使用循环 + `asyncio.sleep` 做轮询，**整个同步工作流仍受 10 分钟总超时约束**；要突破必须走「异步任务」形态（24 小时）。所以「提交任务 → 轮询 → 拿结果」这套范式在 Coze 里是**可行的，但只在异步任务形态下才真正突破时长**。

### 3.3 代码节点（Python/JS）能力边界 —— 结论

| 问题 | 结论 |
|---|---|
| 支持语言 | **JavaScript** 与 **Python** |
| JS 运行时 | V8 **11.3.244.8**（对应 Node.js **20.3.1**），兼容 ECMAScript 2022，支持 TypeScript 编码体验 |
| Python 版本 | **Python 3.11.3 标准库** |
| 能否装第三方库？ | **不能**。JS 仅内置 `dayjs`(1.8.36)、`lodash`(4.17.20)；Python 仅内置 `requests_async`、`numpy` |
| 能否 `pip install`？ | **不能**。官方原话：「引入其他依赖的语句不生效」「不支持使用除 requests_async、numpy 以外的第三方依赖库」 |
| 节点超时 | **单请求限制 60s**，可在 **0.1s~60s** 调 |
| 能否跑 ffmpeg？ | **不能**（无法安装任何外部二进制/依赖，且 `subprocess`/`os` 级别的能力未被官方承诺；`multiprocessing`、`threading`、`sockets`、`pty`、`tty`、`fcntl` 等模块明确不支持） |
| 能否跑 whisper？ | **不能**（whisper 需要 torch/onnx + 模型文件 + 依赖安装，且 `sockets`/`multiprocessing` 被禁；官方仅允许 numpy + requests_async） |
| 代码节点并发 | 每个工作空间 **500** |
| 每个工作流代码节点数 | 最多 **50** 个 |

证据：

> - 节点执行超时：**单请求限制 60s**。
> - 暂不支持 HTTP/HTTPS 以外的其他协议。
> - 每个低代码工作流中最多添加 50 个代码节点。
> —— https://docs.coze.cn/guides_code_node

> **JavaScript**：支持 V8 引擎的 11.3.244.8 版本（对应 Node.js 20.3.1 版本），并兼容 ECMAScript 2022 语法。
> 在 JavaScript 中，**仅内置了两个三方依赖库**：`dayjs`（版本 1.8.36）、`lodash`（版本 4.17.20）。
> `WebCrypto（算法仅支持 AES、HMAC、SHA）`
> —— https://docs.coze.cn/guides_code_node

> **Python**：基于 **Python 3.11.3 的标准库**，大多数模块都能正常运行。**不支持的模块包括** `curses`、`dbm`、`ensurepip`、`fcntl`、`grp`、`idlelib`、`lib2to3`、`msvcrt`、`pwd`、`resource`、`syslog`、`termios`、`tkinter`、`turtle.py`、`turtledemo`、`venv`、`winreg`、`winsound`、`multiprocessing`、`threading`、**`sockets`**、**`pty`** 和 **`tty`**。
> 在 Python 环境中，**仅内置了两个第三方依赖库：`requests_async` 和 `numpy`**。
> - Python 运行时**暂不支持 Http.client 方式的请求**。
> - **不支持使用除 `requests_async`、`numpy` 以外的第三方依赖库。**
> —— https://docs.coze.cn/guides_code_node

> **可以在代码节点中引入依赖包吗？** 代码节点**仅支持引用以下第三方依赖，引入其他依赖的语句不生效**。
> - JavaScript：dayjs、lodash
> - Python：requests_async 、numpy
> —— https://docs.coze.cn/guides_code_node

> 代码节点中，推荐使用 Python 标准库的 `asyncio.sleep` 函数，**`time.sleep` 函数会阻塞工作流执行、导致触发限流**。
> —— https://docs.coze.cn/guides_code_node

**对比：IDE 插件（云侧插件的一种，代码托管在 Coze IDE）能力更强**

> 扣子编程 **IDE 是扣子编程提供的在线编码和运行环境**，允许你在多种技术栈中创建插件。...**插件发布后，IDE 会帮助你托管运行代码**，你无需关心环境配置、服务部署等步骤。
> IDE 运行时：选择 **Node.js** 或者 **Python3**。
> （可选）在 IDE 左下角**依赖包**区域，管理依赖包，所有工具共用该依赖列表。你可以...**输入依赖包名称并选择版本**...然后安装依赖包。
> —— https://docs.coze.cn/guides_ide

> | 依赖包总大小 | 开发插件时，支持添加的依赖包总大小上限为 **250 MB**。 |
> —— https://docs.coze.cn/guides_plugin

> 创建 IDE 插件时，**只支持在 `/tmp` 目录下读写临时文件，文件最大为 512MB**。
> **通过自定义插件实现音视频转换，图片和视频怎么输出？** 通过自定义插件转换音视频时，如果要输出图片、视频等多模态内容，**你需要先将这类文件上传到云存储等文件服务器，获取文件的公开访问 URL，然后再进行输出**。
> —— https://docs.coze.cn/guides_plugin_faq

> **未能验证**：官方**未公布** IDE 插件的运行时镜像里是否预装 ffmpeg，也未公布 IDE 插件的单次调用超时/内存上限（云侧插件超时以「插件节点」的 180s/600s 为准，但 IDE 插件作为插件被调用时是否另有平台侧硬限，文档未说明）。因此**不能断言 IDE 插件能跑 ffmpeg**；且即使能跑，也需要把产物上传到公网可访问的云存储才能输出。

### 对 LinkWork 的影响

- **不要指望用代码节点跑 ffmpeg / whisper**。官方白名单只有 `numpy` + `requests_async`（Python）或 `dayjs` + `lodash`（JS），且 `sockets`/`multiprocessing`/`threading` 被禁。LinkWork 里任何涉及音频解码、ASR、浏览器签名的重活，**必须留在 LinkWork 自己的本地服务里**。
- 如果 LinkWork 需要长耗时（>60s）的本地处理，**必须用「提交任务 + 轮询」两段式**，且注意：
  - 同步工作流总超时 **10 分钟**；
  - 插件节点单节点 **3 分钟默认 / 10 分钟上限**；
  - 想真正跑几十分钟，必须用**异步任务 + 异步任务节点**形态（24 小时）。
- 官方现成的 ASR 能力可以直接用（可能比自建 whisper 更省事）：见第 4 节。

---

## 4. 工作流输入输出类型：文件音频 / 大文本

### 结论

| 问题 | 结论 |
|---|---|
| 能否传「文件」（音频文件）？ | **可以**。文件类型支持音频：**WAV、MP3、FLAC、M4A、AAC、OGG、WMA、MIDI**。上传接口单文件最大 **512MB**。 |
| 音频上传的具体限制 | 上传 API 允许 **512MB**；但**官方语音识别插件单独限制：音频时长 < 30 分钟、文件大小 < 10MB** |
| 工作流节点输入/输出上限 | 均为 **10MB**；**代码节点、插件节点入参上限 2MB**；工作流请求总大小 **20MB** |
| 能否返回大文本（几千到几万字）？ | **可以**。结束节点可返回文本并支持**流式输出**。几千到几万字（以 UTF-8 中文约 3 字节/字计，几万字约几十~百KB）**远低于 10MB 输出上限**，因此结论是可行。 |
| 是否有明确字符数上限？ | **未能验证**。官方没有给「工作流输出最大字符数」。可用的替代界是**字节界**（节点输出 10MB）。另有一个相关但不同的数字：用户变量存储最大约 **6 万字符**（是「用户变量」，不是工作流输出）。 |

### 证据

**（a）文件上传支持音频，单文件 512MB**

> | 文件大小 | 该 API 允许上传的最大文件大小为 **512 MB**。然而，在与智能体对话时，实际可使用的文件大小取决于智能体的模型版本。 |
> | 有效期 | 普通上传的文件将保存在扣子编程服务端，有效期为 **3 个月**。 |
> | 使用限制 | 调用此接口上传文件只能获得文件的 `file_id`，如需获取文件的 URL，建议将文件上传到专业的存储工具中。**不支持下载已上传的文件**。用户仅可在**对话、工作流、端插件、RTC 和 WebSocket** 中通过 `file.id` 访问和使用文件。 |
> | API 流控 | 个人版：10 QPS；企业版：20 QPS。 |
>
> 支持的文件格式：
> | **音频** | **WAV、MP3、FLAC、M4A、AAC、OGG、WMA、MIDI** |
> | 视频 | MP4、AVI、MOV、3GP、3GPP、FLV、WEBM、WMV、RMVB、M4V、MKV |
> | 文档 | DOC、DOCX、XLS、XLSX、PPT、PPTX、PDF、Numbers、CSV |
> —— https://docs.coze.cn/developer_guides_upload_files

注意这条对 LinkWork 很重要：**`file_id` 只能在「对话、工作流、端插件、RTC、WebSocket」中通过 `file.id` 使用，且不支持下载**。也就是说，Coze 的文件是个「内部句柄」，不是通用文件服务。

**（b）官方 ASR 插件的音频硬限**

> - 扣子主账号内的所有子账号共享语音识别的并发限制，**最大并发数为 10**。
> - 音频文件需满足如下要求：
>   - 大模型语音识别的文件格式：opus、ogg、mp3、wav、m4a、mp4、pcm、raw、spx、aac、amr
>   - 小模型语音识别的文件格式：ogg、mp3、wav、mp4、m4a
>   - **音频时长：小于 30 分钟。**
>   - **文件大小：小于 10 MB。**
> —— https://docs.coze.cn/guides_asr_plugin

ASR 插件入参是 `audio_url`：

> | `audio_url` | 必选 | 待转换为文本的音频。你可以引用开始节点的输入、上游节点的输出、用户变量、应用变量、系统变量，实现音频文件的动态输入。 |
> —— https://docs.coze.cn/guides_asr_plugin

**（c）工作流输入输出**

开始节点（官方原文）：

> 开始节点支持配置 `String`、`Number`、`Array<String>`、`Object` 等多种类型的输入参数。
> - Object 类型的参数最多支持 **3 层嵌套**。
> - 如需上传多张图片，可将数据类型设置为 `Array<Image>`，直接上传本地文件，或设置为 `Array<String>`，输入公网可访问图片 URL
> —— https://docs.coze.cn/guides_start_end_node

结束节点（官方原文）：

> **返回文本**模式下，工作流运行结束后，智能体中的模型将直接使用指定的内容回复对话。回答内容中支持引用输出参数，也可以**设置流式输出**。
> 回答内容 | 工作流的最终输出内容，**不可设置为空**。
> 支持开启**流式输出**。...**流式输出适用于输出文本较长**或需要工作流即时反馈的场景
> —— https://docs.coze.cn/guides_start_end_node

字节层面的限（与第 1 节同源）：

> 工作流节点的输入、输出的上限均为 **10MB**；代码节点、插件节点入参上限为 **2MB**
> 低代码工作流支持的请求大小上限为 **20MB**
> 用户变量存储最大支持约 **6 万字符**，出于性能考虑不建议存储过长的内容。
> —— https://docs.coze.cn/guides_workflow_limits

**（d）知识库场景下的文本长度参考（旁证，非工作流直出）**

> 每个文本类型知识库的分段总数最大为 10000 个。
> **单个知识库分段的最大长度为 5000 字符。**
> —— https://docs.coze.cn/guides_knowledge_limits

### 对 LinkWork 的影响

- **音频输入是官方一等公民**：LinkWork 完全可以「上传音频 → 拿 `file_id`/`audio_url` → 交给官方 ASR 插件 → 拿 `data.text`」。这比自建 whisper 省事得多。
- **但音频有 10MB / 30 分钟硬限**（若走官方 ASR 插件）。LinkWork 若要处理长音频（如 1 小时会议录音），必须先在本地切片。
- **大文本输出没问题**：几万字的输出远在 10MB 之内，且结束节点支持流式输出。所以「返回大文本」不是瓶颈。
- **真正的瓶颈是 `file_id` 不可下载、只存在 3 个月、且只能在 Coze 内部流转**。如果 LinkWork 需要拿到音频文件的公网 URL，Coze 的上传接口**不给 URL**（原文：「如需获取文件的 URL，建议将文件上传到专业的存储工具中」）。LinkWork 需要自带对象存储。

---

## 5. 私有部署 / 自托管与宿主机 127.0.0.1 可达性

### 结论

- **扣子编程（商业版 / 云版）官方明确「暂不支持私有化部署」，只提供 Coze Studio 开源版作为自托管路径。**
- **Coze Studio 开源版**：Apache 2.0，Golang + React，Docker Compose 一键部署（`make web`），官方给出的最低配置仅 **2 Core / 4 GB**，默认访问 `http://localhost:8888`。也提供 **Helm Chart**（`helm/charts/opencoze`）用于 K8s 部署。
- **自托管下插件能否访问宿主机 `127.0.0.1` 上的本地服务？**
  - **容器内的 `127.0.0.1` 是容器自身，不是宿主机**，所以需要 `host.docker.internal`（Linux 需加 `extra_hosts: - "host.docker.internal:host-gateway"`）或宿主机 LAN IP。
  - **有强证据表明 Coze Studio 的插件工具执行确实能触达内网地址**：开源仓库存在已公开的 **SSRF 漏洞 issue #2711**「Unrestricted SSRF in plugin tool execution allows any authenticated user to reach internal-only network services」，并对应 **CVE-2026-92795**。官方 README 也**主动提示**了 SSRF 风险。
  - 因此结论是：**能**（甚至可以说不加防护时是「过于能」），但这是**安全缺陷而非受支持特性**，生产必须自行加网络隔离/白名单。

### 证据

**（a）官方明确不支持私有化部署（云版）**

> ### 扣子编程支持私有化部署吗？
> 扣子编程**暂不支持**私有化部署。扣子编程是一个在线的 AI 智能体和应用开发平台，专注于提供云端服务。**如果需要私有化部署，可使用 Coze Studio 开源版**
> —— https://docs.coze.cn/guides_FAQ

> ### Coze Studio 开源版和扣子编程商业版的区别？
> 扣子编程开源遵循的是业界成熟的 **Open-Core**（开源核心）模式...
> Coze Studio 开源版主要面向个人开发者...我们提供了一个功能完整、不受限制的**单机版**引擎...Coze Studio 开源版支持搭建智能体及其资源等周边的核心能力，**不包括工作空间、企业组织管理、多人协作等能力**
> 多人协作、企业组织管理、高并发下的实时通信（Realtime SDK）、**审计日志**等，这些是典型的企业级需求...我们将这些功能作为扣子编程商业版的核心价值。
> —— https://docs.coze.cn/guides_FAQ

> ### 使用 Coze Studio 开源版，是否安全？
> 只要你正确部署和配置，你的数据、模型均存储在自己部署的环境中，不会上传到扣子官方服务器。
> —— https://docs.coze.cn/guides_FAQ

**（b）Coze Studio 部署方式与硬件要求（官方 README）**

> ## 功能清单
> | 模型服务 | 管理模型列表，可接入 OpenAI、火山方舟 等在线或离线模型服务 |
> | 搭建智能体 | 编排、发布、管理智能体；支持配置工作流、知识库等资源 |
> | 搭建应用 | 创建、发布应用；通过工作流搭建业务逻辑 |
> | 搭建工作流 | 创建、修改、发布、删除工作流 |
> | 开发资源 | 支持创建并管理以下资源：**插件**、知识库、数据库、提示词 |
> | API 与 SDK | 创建会话、发起对话等 OpenAPI；通过 Chat SDK 将智能体或应用集成到自己的应用 |
>
> ### 环境要求
> - 在安装 Coze Studio 之前，请确保您的机器满足以下最低系统要求：**2 Core、4 GB**
> - 提前安装 Docker、Docker Compose，并启动 Docker 服务。
>
> ### 部署步骤
> 1. 获取源码。`git clone https://github.com/coze-dev/coze-studio.git`
> 2. 部署并启动服务。
>    ```Bash
>    cd coze-studio
>    # for macOS or Linux
>    make web
>    # for windows
>    cp ./docker/.env.example ./docker/.env
>    docker compose -f ./docker/docker-compose.yml up
>    ```
> 3. 注册账号，访问 `http://localhost:8888/sign`
> 4. 配置模型：访问 `http://localhost:8888/admin/#model-management`
> 5. 访问 Coze Studio `http://localhost:8888/`
>
> ## License
> 本项目采用 **Apache 2.0** 许可证。
> —— https://raw.giteeusercontent.com/910024445/coze-studio/raw/main/README.zh_CN.md
> （该文件为 `coze-dev/coze-studio` 的 `README.zh_CN.md` 镜像；原仓库地址：https://github.com/coze-dev/coze-studio）

**（c）官方 README 主动提示 SSRF 风险 —— 这是「自托管能访问内网」的最强官方侧面证据**

> [!WARNING]
> 如果要将 Coze Studio 部署到公网环境，建议在部署前评估整体评估安全风险，例如账号注册功能、**工作流代码节点 Python 执行环境**、Coze Server 监听地址配置、**SSRF** 和部分 API 水平越权的风险，并采取相应防护措施。
> —— README.zh_CN.md，同上

**（d）仓库结构与 Helm Chart**

> 仓库根目录包含：`backend`、`common`、`docker`、`docs`、`frontend`、**`helm/charts/opencoze`**、`idl`、`scripts`、`LICENSE-APACHE`、`Makefile`...
> Gitee 镜像页面标注许可证为 **Apache-2.0**
> —— https://gitee.com/910024445/coze-studio
> 独立的 Helm 部署文档：https://deepwiki.com/coze-dev/coze-studio/9.2-kubernetes-helm-deployment

**（e）已公开的 SSRF 漏洞 —— 明确的「可访问内网」证据**

> **Unrestricted SSRF in plugin tool execution allows any authenticated user to reach internal-only network services**
> —— coze-dev/coze-studio Issue #2711，https://github.com/coze-dev/coze-studio/issues/2711

> **CVE-2026-92795: Server-Side Request Forgery (SSRF)** —— coze-studio
> —— https://www.rapid7.com/db/vulnerabilities/cve-2026-92795/
> 另见 https://vulnerability.circl.lu/vuln/cve-2026-92795

> **未能验证**：我未能取到 Issue #2711 的正文细节（沙箱内 `github.com` 与 `raw.githubusercontent.com` 网络被阻断，`web_fetch` 亦失败），因此**无法确认该漏洞是否已被官方修复、修复版本号、以及修复方式（是否加了 SSRF 黑名单 / 是否默认禁止私网地址）**。若要以「自托管可访问宿主机」作为 LinkWork 的方案基础，**必须在目标版本上实测确认**，不能只依赖此 issue。

**（f）`host.docker.internal` 是社区/第三方指南确认的通行做法（非官方 README）**

第三方内网部署实战指南在「接入大模型服务」一节明确写了 Docker 容器访问宿主机的方式：

> Ollama 默认 API 地址：`http://127.0.0.1:11434`
> **如果 Coze 在 Docker 容器中访问宿主机 Ollama，可以使用：`http://host.docker.internal:11434`**
> **Linux 上如果不可用**，可以在 `docker-compose.yml` 中添加：
> ```yaml
> extra_hosts:
>   - "host.docker.internal:host-gateway"
> ```
> 如果使用 Ollama 的 OpenAI 兼容接口，可以配置：`OPENAI_API_BASE=http://host.docker.internal:11434/v1`
> —— https://zovps.com/knowledgebaseview?id=33755 （第三方指南，**非官方文档**，仅作参考）

同一指南也给出了镜像里默认监听地址与端口：

> `curl -I http://127.0.0.1:8080` / Nginx `client_max_body_size 200m;`
> —— 同上

**（g）Docker 环境的默认监听配置（来自仓库 `docker/.env.example` 原文）**

```bash
# Server
export LISTEN_ADDR=":8888"
export MAX_REQUEST_BODY_SIZE=1073741824
export SERVER_HOST="http://localhost${LISTEN_ADDR}"
export USE_SSL="0"
export WEB_LISTEN_ADDR="127.0.0.1:8888" # To enable remote access, use 0.0.0.0:8888.
```
—— `docker/.env.example`，https://raw.giteeusercontent.com/910024445/coze-studio/raw/main/docker/.env.example
（可见 `MAX_REQUEST_BODY_SIZE` 默认 **1 GiB**，`WEB_LISTEN_ADDR` 默认只监听 `127.0.0.1`）

**（h）Coze Studio Plus 不是官方产品**

搜索中出现的 `coze-studio/coze-studio-Plus` 是**社区共创项目**，不是字节官方发行版：

> **熬了一晚上，基于开源的 Coze-Studio 规划了一个 Plus 版，有人一起共创么~** · Issue #212 · coze-dev/coze-studio
> —— https://github.com/coze-dev/coze-studio/issues/212

> **未能验证**：未找到任何官方文档说明扣子专业版/企业版提供「私有化部署」交付。官方 FAQ 的表述是「扣子编程暂不支持私有化部署」，指向 Coze Studio 开源版。

### 对 LinkWork 的影响

- **自托管 Coze Studio 是 LinkWork 保住「本地 Python 服务」的唯一现实路线**：因为自托管环境下，插件请求由**你自己的 Coze Studio 后端**发出，而它和你的本地服务在**同一个网络/同一台宿主机**上，因此可以用 `host.docker.internal:PORT`（或宿主机 LAN IP）直连，**不需要公网域名、不需要 TLS 证书、不需要把服务暴露到公网**。
- **但必须注意三点**：
  1. 容器内的 `127.0.0.1` **不是**宿主机。要用 `host.docker.internal`（Linux 需 `extra_hosts`）或宿主机内网 IP，或干脆 `network_mode: host`。
  2. 这条路径的安全性依赖你自己兜底。仓库有 SSRF 漏洞记录（CVE-2026-92795），官方 README 也把它列为风险项。生产部署应把 Coze Studio 关在内网、加防火墙白名单，不要让插件 URL 完全自由。
  3. **开源版只有「单机版」引擎**：**没有**工作空间、企业组织管理、多人协作、审计日志、Realtime SDK。如果 LinkWork 需要多用户/审计，开源版不满足。
- 自托管代价：需要一台 ≥2C4G 的机器（生产建议 8C/16~32G），要跑 MySQL、Redis、Elasticsearch、Milvus、MinIO、NSQ、etcd 等一整套中间件，并自行接模型（OpenAI / 火山方舟 / Ollama 等）。**运维成本不低**。

---

## 6. 官方浏览器自动化 / 网页抓取能力与登录态数据

### 结论（实事求是，不乐观臆测）

- **扣子云端没有可编程的「浏览器自动化」能力**（没有官方 Playwright/headless Chrome 插件，工作流里也没有浏览器节点）。
- 官方**唯一**带「浏览器」字样的东西是 **扣子编程 Scraper**：一个 **Chrome 扩展**，**仅支持 Chrome**，用途是「**手动**提取网页上的文本内容，将这些内容上传到知识库」。它是**人工采集工具**，**不是自动化 API**，**不能被工作流/插件调用**，也**不返回可编程数据**。
- **扣子云端无法拿到抖音/小红书/B站这类需要登录态 + 浏览器签名的数据。** 原因是硬性的：
  1. 云端插件只能发**普通 HTTP 请求**（插件 = 对第三方 API 的 HTTP 代理），**不能维持登录态 Cookie、不能执行 JS、不能生成浏览器签名**（如抖音 `a_bogus`、小红书 `x-s`、B站 `w_rid`）。
  2. 云侧代码节点沙箱 **明确禁用 `sockets`（及 `threading`/`multiprocessing`）**，无法运行真正的浏览器。
  3. IDE 插件虽然能装依赖（≤250MB），但官方**未承诺**运行环境可跑 headless browser，且其**出网能力**（能否访问外部站点）官方未说明；**未能验证**。
- **可编程的正路只有两条**：
  - **① 官方开放平台授权 API**（如抖音开放平台确实有 Create Video 等 OpenAPI），走**授权态**而非**抓取态**。
  - **② 第三方数据服务**：插件做薄 HTTP 代理，把登录态/签名/反爬交给外部付费服务处理。
- **没有任何官方证据**表明扣子插件商店里有官方出品的「浏览器自动化/网页抓取」插件可以绕过登录态与签名。

### 证据

**（a）官方 Scraper = 手动 Chrome 扩展，仅用于知识库**

> ## 安装 Scraper
> **扣子编程 Scraper 是一个用来提取网页上的文本内容，将这些内容上传到知识库的一个工具。**你可以通过 Chrome 浏览器的应用商店进行安装。
> 说明：**扣子编程 Scraper 目前仅支持 Chrome 浏览器。**
> ...
> ## 使用扣子编程 scraper 采集数据
> 1. 登录扣子开发平台...
> 4. 在页面右上角，选择**添加内容** > **在线数据**。
> 5. 单击**手动采集**，然后在弹出的页面点击**权限授予**完成授权。
> 6. 在弹出的页面**输入要采集内容的网址**，然后单击**确认**。
> 7. 在弹出的页面上，**点击页面下方文本标注按钮，开始标注要提取的内容**
> 7. 单击**查看数据**查看已采集的内容，确认无误后再点击**完成并采集**。
> —— https://docs.coze.cn/guides_scraper

这条文档明确显示：Scraper 是**人在浏览器里手动点选标注**的工具，产出是「知识库内容」，**没有 API、没有工作流节点**。

**（b）云端插件 = 对第三方 HTTP API 的代理，不做浏览器行为**

> 插件是一个工具集，一个插件内可以包含一个或多个工具（**API**）。
> 「每个插件可添加多个工具...插件中的每个工具都是一个独立的 **API**。智能体调用插件时，实际上是调用插件中的某个工具，也就是**调用某个 API**。」
> —— https://docs.coze.cn/guides_plugin

> 通过自定义插件转换音视频时，如果要输出图片、视频等多模态内容，你需要先将这类文件上传到云存储等文件服务器，获取文件的公开访问 URL，然后再进行输出。
> —— https://docs.coze.cn/guides_plugin_faq

**（c）官方 FAQ 承认「链接读取插件」的能力边界（间接说明云端抓取很弱）**

> **为什么链接读取插件无法读取内容，输出 `null`？** 使用链接读取插件时，请检查输入的 URL 是否正确。当 URL 中混杂其他非 URL信息时，系统将无法正常解析，输出结果可能为 `null`。
> **链接读取插件读取内容的语言不一致，如何处理？** 直接打开页面时为中文内容，通过链接读取插件读取的内容为英文内容，此时你可以在 URL 中添加 query 参数 `lang=zh-CN`。
> —— https://docs.coze.cn/guides_plugin_faq

「语言不一致要靠加 `lang=zh-CN` query 参数」这一点，侧面说明 LinkReader 走的是**静态 HTTP 抓取**，而非渲染后取内容。官方文档**全文未承诺** LinkReader 支持 JS 渲染或登录态页面。

**（d）代码节点沙箱禁 `sockets`，无法跑浏览器**

> 不支持的模块包括 ... `multiprocessing`、`threading`、**`sockets`**、`pty` 和 `tty`。
> —— https://docs.coze.cn/guides_code_node

**（e）抖音官方开放平台确实有 OpenAPI（这是「授权态」正路）**

> 创建视频 —— `open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video/`
> —— https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video/

**（f）搜索到的「扣子 + 小红书/抖音数据」文章，均为第三方教程/自媒体内容，非官方能力**

例如：
- 「感谢扣子空间让我成为小红书数据专家」—— https://ai.aiking123.com/146807.html （自媒体博客）
- 「从零开始利用 coze 智能体 API 提取抖音视频文本内容」—— https://www.cnblogs.com/shuinanxun/p/18586887 （社区文章）
- 「如何使用扣子和抖音开放平台 API 实现短视频热点视频选题推送」—— https://www.php.cn/faq/2764212.html （社区文章，走的是抖音开放平台 API，印证了「授权态」路线）

**（g）开源自托管仓库中有一个 `feat/browser_use_plugin` 分支**

在 coze-studio 的 Gitee 镜像分支列表中可见分支名 **`feat/browser_use_plugin`**（另有 `fix/coderunner_rce`、`fix/csrf`、`fix/mrh/oauth_phishing` 等）。

> 分支 (44)：main、fix/mrh/oauth_phishing、fix/csrf、fanlv、release/v0.5.1、**feat/browser_use_plugin**、feat/conversation-isolation、release/v0.5.0、...
> —— https://gitee.com/910024445/coze-studio

> **未能验证**：该分支是**开发中的实验性分支**，我没有取到它的代码内容或任何官方文档说明（`github.com` 在本沙箱被阻断）。因此**不能据此认为 Coze 已提供可用的浏览器自动化能力**。仅作为「官方团队在探索 browser use」的弱信号记录。

### 对 LinkWork 的影响

这一节对 LinkWork 是**最重要的战略结论**：

- **LinkWork 的核心资产恰恰是「浏览器插件 + 登录态」**——而这正是**扣子云端最缺、也最难补的能力**。扣子云端拿不到需要登录态 + 浏览器签名的数据，这是**架构性限制，不是配置问题**。
- 所以：
  - **不要把 LinkWork 的价值押在「让扣子云端去抓抖音/小红书/B站」上**。这条路在云版上走不通（拼图缺的正是你手里那块）。
  - **正确的定位是反过来**：LinkWork 作为**数据获取层**（浏览器插件持有登录态、生成签名、抓取数据），把**结构化结果**通过**端插件**（见第 2 节）或**自托管 Coze Studio 的插件**（见第 5 节）喂给 Coze，让 Coze 负责 **LLM 推理 / 编排 / 生成**。
  - 也就是说，LinkWork 不是「被改造成扣子插件」，而更应该是「**扣子的宿主 + 数据源**」。这与第 2 节路线 B、第 5 节自托管路线的结论一致。

---

## 7. 本机 Coze CLI 现状与 `--type` 支持

### 7.1 本机安装与登录状态

**结论：`coze` CLI 已安装，版本 0.3.10，且已登录（PAT，永不过期）。**

命令与输出摘录：

```console
$ which coze
/opt/homebrew/bin/coze

$ coze --version
0.3.10
```

```console
$ coze auth status --format json
{
  "logged_in": true,
  "user": {
    "user_id": "2130167522135292",
    "user_name": "user4085567148",
    "nick_name": "踏山",
    "avatar_url": "https://res.volccdn.com/obj/volc-console-fe/vconsole-static/cloudidentity.avartar-huoshan.9f69c68a.jpg"
  },
  "token_expires_at": "Never (PAT)"
}
```

（`coze auth status` 的 text 模式输出为：`logged_in true` / `token_expires_at Never (PAT)`。）

### 7.2 `coze code project create --type` 支持哪些类型

**结论：支持 `agent | workflow | app | skill | web | webapp | miniprogram | assistant | general` —— 共 9 个值（源码另兼容 `webapp`）。**

- ✅ **支持 `workflow`**（工作流）。
- ❌ **不支持 `plugin` / `插件`** —— 受支持列表里**没有** plugin 或 tool 这个类型。
- ⚠️ 最接近「插件」的是 **`skill`**：本机 skill 文档把「技能 / skill 插件」映射为 `--type skill`，但那是 **Coze Coding 的 Skill 项目**，**不等于**「扣子插件/工具（Plugin/Tool）资源」。**两者不要混淆。**

`coze code project create --help` 关键片段（原文摘录）：

```
@HELP coze code project create

BRIEF: Create a new Coze Coding project

SYNTAX:
  coze code project create [options]

OPTIONS:
  Name              Type        Required  Default     Description
  ─────────────────────────────────────────────────────────
  --message <message>  string      Yes       -           Description/requirements for creating the project
  --type <type>     string      Yes       -           Project type: agent | workflow | app | skill | web | webapp | miniprogram | assistant | general
  --org-id <orgId>  string      No        -           Organization ID (optional, will read from context automatically)
  --space-id <spaceId>  string      No        -           Space ID (optional, will read from context automatically)
  --wait            flag        No        -           Wait for project creation to complete
  --chat-mode <chatMode>  string      No        -           Chat mode: ask | agent | dangerous_confirm | plan
  --model-name <modelName>  string      No        -           Model name for the project
  --tool-name <toolName>  string      No        -           Tool names to enable (can specify multiple times)
  --design          flag        No        -           Enable design wizard (auto-enabled when --chat-mode is plan)
  --create-source <source>  string      No        -           Create source set by the using-coze-cli skill: agent_cli (other agent) | coze_agent_cli (Coze/扣子 agent). Defaults to cli (manual). COZE_CLAW_AGENT_ID env forces coze_agent_cli

ENUMS:
  --type:
    agent        AGENT project
    workflow     WORKFLOW project
    webapp       WEBAPP project
    app          APP project
    skill        SKILL project
    web          WEB project
    miniprogram  MINIPROGRAM project
    assistant    ASSISTANT project
    general      GENERAL project

COMMON ERRORS:
  CODE   NAME              DESCRIPTION             FIX
  E1005  INVALID_PROJECT_TYPE Invalid project type   Please use a valid project type: agent | workflow | app | skill | web | webapp | miniprogram | assistant | general
  E1006  UNSUPPORTED_PROJECT_TYPE Project type is not supported for creation Please choose a supported project type and try again
```

`coze code project --help` 关键片段：

```
AVAILABLE COMMANDS:
  create      Create a new Coze Coding project
  list        List all projects in the current space
  get         Get project details
  delete      Delete project
  db          Manage Coze Coding projects
  import      Import a project from a remote repo or local zip file
```

`coze plugin --help` 关键片段（**注意：这个 `plugin` 是 CLI 自身的功能开关，不是扣子插件资源！**）：

```
@HELP coze plugin

BRIEF: Manage CLI plugins

DESCRIPTION:
  Install, uninstall and list CLI plugins. Plugins are opt-in: a feature is only enabled after its plugin is installed.

AVAILABLE COMMANDS:
  list        List all plugins
  install     Install a plugin
  uninstall   Uninstall a plugin

COMMON EXAMPLES:
  coze plugin list
  coze plugin install default-space
  coze plugin uninstall default-space
```

> ⚠️ **避坑**：`coze plugin install` 安装的是 **CLI 本地功能开关**（如 `default-space`），**与扣子平台的「插件/工具」资源完全无关**。不要误以为用它就能创建/部署一个扣子插件。

### 7.3 本机技能文档中与 workflow / plugin 相关的支持情况

依据 `/Users/jerod/.agents/skills/using-coze-cli/coze-code/MODULE.md`：

> **`--type` 接受**：`agent | workflow | app | skill | web | miniprogram | assistant | general`（源码 `resolveProjectType` 还兼容 `webapp`）。
> 传入未知类型会报 `E1005`。
> —— `/Users/jerod/.agents/skills/using-coze-cli/coze-code/MODULE.md`，第 71 行

> | 意图 | 推荐命令 | 备注 |
> | 创建新项目 | `coze code project create` | ...`--type` 支持 agent/workflow/app/skill/web/miniprogram/assistant/general（常用 web/app；general 仅用户点名时用） |
> —— 同文件，第 306 行

> | "工作流 / 自动化流程" | `workflow` |
> | "技能 / skill 插件" | `skill` |
> —— 同文件，第 92–93 行（项目类型判定表）

依据 `/Users/jerod/.agents/skills/using-coze-cli/coze-code/references/coze-code-project.md`：

> | `--type` | 是 | 项目类型：`agent` / `workflow` / `app` / `skill` / `web` / `miniprogram` / `assistant` / `general`（源码另兼容 `webapp`）。`general` 为后端泛化类型，仅在用户明确点名时使用 |
> —— `references/coze-code-project.md`，第 31 行

> - **`--type` 仅接受受支持列表内的值**，传入未知类型报 `E1005 Unknown project type`；`web` / `app` 最常用，`assistant` 走模板复制创建。
> —— 同文件，第 102 行

依据 `/Users/jerod/.agents/skills/using-coze-cli/coze-code/MODULE.md`（技能/插件相关命令）：

> | 挂载/解绑技能 | `coze code skill add/remove` | 对默认会话挂载或解绑 |
> | 列出技能 | `coze code skill list` | 支持 `--my` 列个人技能 |
> | 上传个人技能 | `coze code skill upload <file> -p <id>` | 上传 .skill 文件，冲突自动覆盖；`-p` 必填 |
> | 删除个人技能 | `coze code skill delete <skill-id> -p <id>` | 永久删除技能本体(区别于 remove)；`-p` 必填 |
> —— 同文件，第 321–324 行

> **注意**：`coze code skill *` 操作的是 **Skill（技能）**，不是 **Plugin（插件/工具）**。CLI 中**没有**创建/发布「扣子插件（Plugin/Tool）」的命令族。

### 7.4 CLI 覆盖范围的一个额外发现

CLI 文档中「工具（Tools）」是**会话维度的开关**（如联网搜索、图片生成），也不是「插件开发」：

> | **Tools（工具）** | 项目默认会话启用的工具（如联网搜索、图片生成）。通过 `tools list/enable/disable` 管理（会话维度） |
> —— `/Users/jerod/.agents/skills/using-coze-cli/coze-code/MODULE.md`，第 26 行

### 对 LinkWork 的影响

- **本机 CLI 可用，无需重新登录**（PAT 长期有效），但**它帮不了「把 LinkWork 造成扣子插件」这件事**：
  - `coze code project create` **没有 plugin/tool 类型**。它能创建的是 Coze Coding 项目（web/app/miniprogram/agent/workflow/skill/...），**不是扣子资源库里的插件**。
  - **扣子插件（Plugin/Tool）的创建只能走 Web 控制台**（资源库 → +资源 → 插件），或用 JSON/YAML（OpenAPI/Swagger/Postman）在控制台导入。**CLI 无对应命令。**
- 可用的部分是：
  - `coze code project create --type workflow` 可以**创建工作流项目**（但那是 Code 形态的项目，不是资源库工作流）；
  - `coze code skill upload` 可以上传个人技能。
- **结论**：如果要让 LinkWork 成为扣子插件，**必须使用扣子 Web 控制台或 OpenAPI 导入流程**，CLI 不是载体。CLI 只适合做辅助（比如用 `coze file upload` 把本地音频换成在线链接）。

---

## 硬约束清单

以下为**不可绕过**的限制（除非明确标注为「可绕过 / 有条件」）：

- **云侧插件 URL 必须是域名格式，不支持 IP 格式**（`http://127.0.0.1:8000` 在格式层面即被拒）。—— 不可绕过
- **云侧插件的 HTTP 请求由 Coze 云端服务器发出**（可从固定 IP 白名单功能反证），因此**访问不到用户本机的 `127.0.0.1` / `localhost` / 内网**。—— 不可绕过
- **同一插件内的不同工具必须使用相同域名**；多 API 导入时所有 API 必须有相同 URL 路径前缀，否则导入失败。—— 不可绕过
- **云侧插件默认动态出口 IP**；固定 IP 白名单仅对**企业旗舰版白名单用户**开放，且**仅支持基于已有服务创建的云侧插件**（IDE 云侧插件与端侧插件不支持）。—— 不可绕过
- **云侧插件要走私网（VPC）只能使用「私网模式插件」**，其门槛为：**仅企业旗舰版/原企业版** + **API 服务必须部署在火山引擎华北 2 地域** + **不支持发布到模板和插件商店** + **只支持「基于已有服务创建」**。—— 不可绕过
- **插件节点默认超时 180s（3 分钟），可调范围 0.1s ~ 600s（10 分钟）**。—— 不可绕过（上限 10 分钟）
- **`端插件` 类型的插件节点无超时时间限制**。—— 这是唯一的超时豁免，但代价见下一条
- **端插件（客户端插件）仅支持通过 API 方式使用**（`v3/chat` 的 `requires_action` + `submit_tool_outputs`）；**扣子页面内无法调试**；**暂不支持在大模型节点中配置端插件技能**。（工作流中可添加端插件节点）—— 不可绕过
- **端插件 `requires_action` 状态 24 小时未提交结果即 `expired`**。—— 不可绕过
- **代码节点单请求超时硬上限 60s，可调范围仅 0.1s ~ 60s（无法调高）**。—— 不可绕过
- **代码节点禁用 `sockets`、`threading`、`multiprocessing`、`pty`、`tty`、`fcntl`、`subprocess` 相关能力**（`multiprocessing`/`threading`/`sockets` 在官方不支持列表中）。—— 不可绕过
- **代码节点无法安装第三方库**：Python 仅 `requests_async` + `numpy`（Python 3.11.3 标准库）；JavaScript 仅 `dayjs` + `lodash`（Node 20.3.1 / V8 11.3.244.8）。官方原话「引入其他依赖的语句不生效」。—— 不可绕过
- **代码节点无法运行 ffmpeg / whisper**（既不能装依赖，也不能起进程，`sockets`/`multiprocessing` 被禁）。—— 不可绕过
- **代码节点 `time.sleep` 会阻塞工作流并触发限流**，只有 `asyncio.sleep` 可用，**最长延迟 1 分钟**。—— 不可绕过（这直接限制了「轮询」的单次等待粒度）
- **工作流整体超时：同步运行 10 分钟；异步运行 24 小时**。未开异步时官方建议控制在 5 分钟以内。—— 不可绕过
- **「工作流暂不支持异步插件节点」**：若插件是异步的，必须用**循环节点 + 代码节点 `asyncio.sleep`** 自行实现异步等待与结果轮询。—— 不可绕过
- **工作流节点数量上限 1000 个 / 每次执行最多 1000 个节点（含循环次数与子工作流节点）**。—— 不可绕过
- **每个工作流最多 50 个代码节点**。—— 不可绕过
- **工作流请求大小上限 20MB；节点输入/输出上限 10MB；代码节点与插件节点入参上限 2MB**（超限报 `Invalid Request: input size exceeded` 或被省略号截断）。—— 不可绕过
- **试运行 IDE 插件节点时输出上限 64KB**。—— 不可绕过（仅试运行）
- **插件总数：每工作空间最多 1000 个插件；每账号最多 30 个 IDE 插件；每插件最多 100 个工具；自定义插件 QPS 上限 50；依赖包总大小上限 250MB**。—— 不可绕过
- **代码节点并发限制为每工作空间 500**；单节点 QPS 上限 3000（数据库节点 400）；单工作流 QPS 上限 200（个人免费版）~500（付费）**，且主账号与其所有子账号共享**。—— 不可绕过
- **一次对话中，智能体连续两次回复的时间间隔限制为 10 分钟**；扣子商店/调试区单次对话请求超时 10 分钟（其他渠道无此限制）。—— 不可绕过
- **官方 ASR 插件音频硬限：时长 < 30 分钟、文件大小 < 10MB、并发 10**。—— 不可绕过
- **Coze 上传文件单文件上限 512MB；有效期 3 个月；只能拿到 `file_id`，官方明确「不支持下载已上传的文件」，且「如需获取文件的 URL，建议将文件上传到专业的存储工具中」**。—— 不可绕过
- **`file_id` / `file.id` 只能在对话、工作流、端插件、RTC、WebSocket 中使用**。—— 不可绕过
- **用户变量存储最大约 6 万字符**（官方不建议存过长内容）。—— 不可绕过
- **扣子编程（商业云版）官方明确「暂不支持私有化部署」**。私有化只能走 **Coze Studio 开源版**。—— 不可绕过
- **Coze Studio 开源版是「单机版」引擎，不包括工作空间、企业组织管理、多人协作能力**；审计日志、Realtime SDK 等属商业版核心价值。—— 不可绕过
- **Coze Studio 开源版最低硬件要求 2 Core / 4 GB**（生产建议 8C/16~32G），需 Docker + Docker Compose，并需自行部署 MySQL、Redis、Elasticsearch、Milvus、MinIO、NSQ、etcd 等中间件并配置模型服务。—— 属于部署成本，非功能限制
- **自托管 Coze Studio 中，容器内的 `127.0.0.1` 不是宿主机**；要访问宿主机本地服务需用 `host.docker.internal`（Linux 需在 compose 中加 `extra_hosts: - "host.docker.internal:host-gateway"`）或宿主机 LAN IP。—— 可绕过（改用正确地址即可）
- **自托管 Coze Studio 存在已公开的 SSRF 风险（Issue #2711 / CVE-2026-92795），官方 README 主动提示 SSRF 与代码节点 Python 执行环境风险**。「能访问内网」是**安全缺陷而非受支持特性**，生产必须自行加网络隔离与白名单。修复状态**未能验证**。—— 需自行防护
- **扣子云端无官方「浏览器自动化」能力**（无官方 Playwright/headless Chrome 插件，工作流无浏览器节点）。—— 不可绕过
- **官方 Scraper 是仅支持 Chrome 的浏览器扩展，用途是「手动提取网页文本并上传知识库」，不是可编程 API，不能在工作流/插件中调用**。—— 不可绕过
- **扣子云端拿不到需要登录态 + 浏览器签名的抖音/小红书/B站数据**（插件只能发普通 HTTP 请求；代码节点禁用 `sockets` 无法跑真浏览器）。这是架构性限制。—— 不可绕过
- **可编程的正路只有：官方开放平台授权 API（如抖音开放平台 OpenAPI），或由外部第三方数据服务持有登录态/签名、插件只做薄 HTTP 代理**。—— 属可行路径
- **`coze code project create --type` 不支持 `plugin`**；受支持值为 `agent | workflow | app | skill | web | webapp | miniprogram | assistant | general`，传其它值报 `E1005`。—— 不可绕过
- **CLI 中不存在创建/发布「扣子插件（Plugin/Tool）」的命令族**；`coze plugin *` 是 CLI 自身功能开关（如 `default-space`），与平台插件无关；`coze code skill *` 操作的是 Skill，不等于 Plugin。扣子插件只能走 Web 控制台或 OpenAPI 导入。—— 不可绕过
- **未能验证项（务必实测，不要当结论用）**：
  - 官方**没有**「插件响应体大小上限」的明文数字（只有插件/代码节点入参 2MB、节点输出 10MB、IDE 试运行 64KB 等间接界限）。
  - 官方**没有**「工作流输出最大字符数」的明文数字（只有 10MB 字节界与「用户变量 6 万字符」）。
  - 官方**未公布** IDE 插件运行环境是否预装 ffmpeg、IDE 插件的出网能力、以及 IDE 插件作为工具被调用时的独立超时/内存上限。
  - **未能取到** Coze Studio Issue #2711 正文，无法确认 SSRF 是否已修复、修复版本与方式。
  - coze-studio 的 `feat/browser_use_plugin` 分支内容未知，**不能**据此认为已有可用的官方浏览器自动化能力。
  - 未找到任何官方文档说明扣子专业版/企业版提供「私有化部署」交付。

---

## 附：参考来源汇总

**官方文档（docs.coze.cn）**
- [低代码工作流使用限制](https://docs.coze.cn/guides_workflow_limits) —— 超时总表、QPS、节点数、请求/节点大小、用户变量长度
- [插件介绍](https://docs.coze.cn/guides_plugin) —— 插件分类、使用限制（数量/QPS/依赖包）
- [基于 API 创建插件](https://docs.coze.cn/guides_services) —— 插件 URL 必须域名、授权方式
- [通过 JSON 或 YAML 文件导入插件](https://docs.coze.cn/guides_import) —— OpenAPI/Swagger/Postman、OpenAPI 3.0.1 示例
- [使用 IDE 创建插件](https://docs.coze.cn/guides_ide) —— IDE 插件、Node/Python3 运行时、依赖包
- [创建私网模式插件](https://docs.coze.cn/guides_private_plugin) —— 企业旗舰版、华北 2、VPC
- [通过固定 IP 访问插件](https://docs.coze.cn/guides_fixed_ip_access_plugin) —— 云端出口 IP
- [插件常见问题](https://docs.coze.cn/guides_plugin_faq) —— /tmp 512MB、多模态需云存储 URL、IP 地址调试失败
- [端插件概述](https://docs.coze.cn/guides_local_plugin_overview) —— 端插件机制、requires_action、仅 API、24h
- [创建端插件](https://docs.coze.cn/guides_create_local_plugin) —— 读本地目录/文件示例、JSON/YAML 导入
- [代码节点](https://docs.coze.cn/guides_code_node) —— 语言/版本/依赖白名单/禁 sockets/60s/异步轮询
- [异步执行低代码工作流](https://docs.coze.cn/guides_execute_workflow_asynchronously) —— 异步任务、24h、队列
- [开始和结束节点](https://docs.coze.cn/guides_start_end_node) —— 输入类型、结束节点返回文本/流式输出
- [插件节点](https://docs.coze.cn/guides_plugin_node) —— 插件节点超时 180s/0.1~600s
- [语音识别插件](https://docs.coze.cn/guides_asr_plugin) —— 音频格式/30min/10MB/并发 10
- [安装 Scraper](https://docs.coze.cn/guides_scraper) —— 仅 Chrome、手动采集、知识库
- [上传文件](https://docs.coze.cn/developer_guides_upload_files) —— 512MB、3 个月、file_id 不可下载、音频格式
- [知识库使用限制](https://docs.coze.cn/guides_knowledge_limits) —— 分段 5000 字符等
- [常见问题](https://docs.coze.cn/guides_FAQ) —— 私有化部署、开源版 vs 商业版、10 分钟对话超时

**Coze Studio 开源版**
- https://github.com/coze-dev/coze-studio （原仓库；本沙箱内不可直连）
- [README.zh_CN.md 镜像](https://raw.giteeusercontent.com/910024445/coze-studio/raw/main/README.zh_CN.md) —— 功能清单、2C4G、`make web`、Apache 2.0、SSRF 警告
- [docker/.env.example 镜像](https://raw.giteeusercontent.com/910024445/coze-studio/raw/main/docker/.env.example) —— 环境变量默认值
- [CLAUDE.md 镜像](https://raw.giteeusercontent.com/910024445/coze-studio/raw/main/CLAUDE.md) —— 架构、Docker 服务栈、端口 8888
- [Gitee 仓库页（分支/许可证）](https://gitee.com/910024445/coze-studio) —— Apache-2.0、`helm/charts/opencoze`、`feat/browser_use_plugin`
- [Issue #2711: Unrestricted SSRF in plugin tool execution](https://github.com/coze-dev/coze-studio/issues/2711)
- [CVE-2026-92795 (Rapid7)](https://www.rapid7.com/db/vulnerabilities/cve-2026-92795/)
- [CVE-2026-92795 (Vulnerability-Lookup)](https://vulnerability.circl.lu/vuln/cve-2026-92795)
- [Kubernetes Helm Deployment (DeepWiki)](https://deepwiki.com/coze-dev/coze-studio/9.2-kubernetes-helm-deployment)

**第三方参考（非官方，仅作旁证）**
- [Coze 开源版内网部署实战（含 host.docker.internal 用法）](https://zovps.com/knowledgebaseview?id=33755)
- [抖音开放平台 - 创建视频 OpenAPI](https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video/)

**本机文档路径**
- `/Users/jerod/.agents/skills/using-coze-cli/SKILL.md`
- `/Users/jerod/.agents/skills/using-coze-cli/coze-code/MODULE.md`
- `/Users/jerod/.agents/skills/using-coze-cli/coze-code/references/coze-code-project.md`

**本机命令输出**
- `coze --version` → `0.3.10`
- `coze auth status --format json` → `logged_in: true`, `token_expires_at: "Never (PAT)"`
- `coze code project create --help` → `--type` 枚举（不含 plugin）
- `coze plugin --help` → CLI 自身插件管理（与平台插件无关）

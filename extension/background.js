/**
 * LinkWork 扩展：service worker。
 *
 * 职责：
 *  - 点击工具栏图标 → 打开右侧边栏
 *  - 与本地 Python 服务通信（健康检查、提交任务、轮询结果）
 *  - 读取页面数据：通过 chrome.scripting 在页面主世界（MAIN world）执行提取函数
 *  - 维护「侧边栏连接」生命周期：侧边栏打开即建连并周期探活，
 *    侧边栏关闭或浏览器退出即断开（见下方 panelBridge）
 *
 * 设计原则：失败就报错，不做降级猜测。
 */

importScripts('extractor.js');

const SERVICE_URL = 'http://127.0.0.1:17890';
/** 侧边栏长连接的名字，sidepanel.js 必须使用同一个值 */
const PANEL_PORT_NAME = 'linkwork-panel';
/**
 * 面板打开期间的探活间隔。
 * 除了让界面上的状态灯反映真实连接状态，这串心跳还负责让 MV3 service worker
 * 在侧边栏开着时不被浏览器回收（空闲 30s 即回收）——否则用户点「提取文案」时
 * 后台可能已经不在了。
 */
const PANEL_HEARTBEAT_MS = 20000;
/**
 * native messaging host 的名字，必须与 scripts/install_native_host.py 里的一致。
 * 它是「让插件把本地服务拉起来」的唯一通道：扩展无权执行 shell 命令，
 * 只能通过这个已注册的本机程序代办（协议见 native_host/linkwork_host.py）。
 */
const NATIVE_HOST_NAME = 'com.linkwork.host';
/** 自动拉起失败后的重试间隔（比心跳慢，避免反复拉起一个起不来的服务） */
const AUTOSTART_RETRY_MS = 60000;
/** 调用 native host 的超时（首次启动服务通常 1s 内，留足余量） */
const NATIVE_HOST_TIMEOUT_MS = 45000;
/** native host 瞬时失败（启动了但立刻退出）时的尝试次数 */
const NATIVE_HOST_ATTEMPTS = 2;
/** 两次尝试之间的退避（测试里可置 0 以便快速断言） */
let nativeHostRetryDelayMs = 400;

// ---------------------------------------------------------------- 侧边栏
/**
 * 让「点击工具栏图标」直接打开右侧边栏。
 * 注意：必须用 classic service worker（不能声明 "type": "module"），
 * 因为本文件使用 importScripts()，module worker 里没有这个 API。
 */
function enableSidePanelOnActionClick() {
  if (!chrome.sidePanel?.setPanelBehavior) {
    console.error('[LinkWork] 当前浏览器不支持 sidePanel API，需要 Edge/Chrome 114+');
    return;
  }
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error('[LinkWork] 设置侧边栏行为失败：', err));
}

enableSidePanelOnActionClick();
chrome.runtime.onInstalled.addListener(enableSidePanelOnActionClick);

// 兜底：若浏览器没有按上面的行为自动打开，则在图标点击时手动打开。
// 与 setPanelBehavior 并存是安全的（自动打开生效时本回调不会被触发）。
chrome.action.onClicked.addListener(async (tab) => {
  if (!chrome.sidePanel?.open) return;
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch (err) {
    console.warn('[LinkWork] 手动打开侧边栏失败（通常可忽略）：', err);
  }
});

// ------------------------------------------------- 侧边栏连接（开/关即连/断）
/**
 * 侧边栏打开时由 sidepanel.js 主动 connect 一条长连接，关闭时浏览器自动断开
 * （浏览器退出、扩展重载、页面崩溃同样会断开，语义一致）。
 *
 * 连接存续期间：
 *   - 立刻做一次健康检查并把结果推给面板（面板不用自己再问一次）；
 *   - 之后每 PANEL_HEARTBEAT_MS 探活一次，状态变化才推送；
 *   - 断开时清掉定时器，连接对象从表里移除。
 *
 * 注意：这里只负责「连接」的开关，不会去启动/停止本地服务进程
 * （浏览器扩展无权拉起本机进程，服务进程需由 `linkwork serve` 常驻）。
 */
const panelLinks = new Map(); // port -> { port, windowId, timer, inflight, lastOk }

/** 让 connect 消息与 sidepanel.js 彻底解耦：对方只认字符串，不需要数字枚举 */
const HEALTH_OK = 'ok';
const HEALTH_FAIL = 'fail';
const HEALTH_CHECKING = 'checking';

/** 健康检查结果 → 面板可读的状态描述 */
function describeHealth(result) {
  if (!result || result.status === HEALTH_FAIL) {
    return {
      status: HEALTH_FAIL,
      text: '服务未连接',
      title: result?.error
        ? `${result.error}（自动重连中）`
        : '无法连接本地服务，自动重连中',
    };
  }
  if (result.status === HEALTH_CHECKING) {
    return { status: HEALTH_CHECKING, text: '检查服务…', title: '正在检查本地服务' };
  }
  const asr = (result.asr || []).find((entry) => entry.available);
  const name = result.serviceName || 'LinkWork 本地服务';
  const title = [
    `已连接 ${SERVICE_URL}`,
    `版本 v${result.version || '?'}`,
    `语音识别：${asr ? asr.name : '不可用'}`,
    `最近检查：${result.checkedAt || ''}`,
  ].join('\n');
  return {
    status: HEALTH_OK,
    text: `已连接：${name}${asr ? ` · ${asr.name}` : ''}`,
    title,
  };
}

/** 统一向某条连接推消息；连接已断开时忽略（不能因此抛错打断查询流程） */
function postPanelMessage(link, message) {
  try {
    link.port.postMessage(message);
  } catch {
    /* 对端已关闭：onDisconnect 会负责清理 */
  }
}

/** 查询本地服务健康状态，永远返回结构化结果（不抛异常） */
async function probePanelService() {
  try {
    const health = await serviceRequest('/api/health', 'GET', null, 4000);
    return {
      status: HEALTH_OK,
      version: health?.version || '',
      asr: Array.isArray(health?.asr) ? health.asr : [],
      serviceName: health?.name || '',
      checkedAt: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    };
  } catch (err) {
    return { status: HEALTH_FAIL, error: describeError(err) };
  }
}

/** 探活一次并（仅状态变化时）推送给面板 */
async function refreshPanelLink(link) {
  if (link.inflight) return; // 上一轮还没回来，不叠加请求
  link.inflight = true;
  try {
    let result = await probePanelService();
    // 服务没跑 + 自动启动开关为开 → 通过 native host 把它拉起来，再探一次
    if (result.status !== HEALTH_OK && await shouldAutoStart(link)) {
      postPanelMessage(link, {
        type: 'service:state',
        status: HEALTH_CHECKING,
        text: '正在启动本地服务…',
        title: '已通过本机程序启动 linkwork serve，首次启动需要几秒',
      });
      const started = await startServiceViaHost(link);
      if (!started.ok) {
        result = { status: HEALTH_FAIL, error: started.error };
      } else {
        result = await probePanelService();
      }
    }
    const next = describeHealth(result);
    const changed = next.status !== link.lastStatus || next.text !== link.lastText;
    link.lastStatus = next.status;
    link.lastText = next.text;
    // 首轮检查（lastStatus 为空）一定推送；之后只在变化时推送，避免刷屏
    if (changed || !link.reported) {
      link.reported = true;
      postPanelMessage(link, { type: 'service:state', ...next });
    }
  } finally {
    link.inflight = false;
  }
}

// ------------------------------------------------- 通过 native host 启动服务
/**
 * 自动启动开关（面板上的勾选框，默认开启）。
 * 存在 chrome.storage.local，重启浏览器后仍然有效。
 */
const AUTO_START_KEY = 'autoStartService';
let autoStartService = true;

async function syncAutoStart() {
  try {
    const stored = await chrome.storage.local.get(AUTO_START_KEY);
    autoStartService = stored?.[AUTO_START_KEY] !== false;
  } catch {
    autoStartService = true;
  }
  return autoStartService;
}

async function setAutoStart(value) {
  autoStartService = Boolean(value);
  try {
    await chrome.storage.local.set({ [AUTO_START_KEY]: autoStartService });
  } catch (err) {
    console.warn('[LinkWork] 保存自动启动开关失败：', err);
  }
  return autoStartService;
}

/** 判断这次探活失败该不该顺手把服务拉起来 */
async function shouldAutoStart(link) {
  if (!autoStartService) return false;
  if (link.startBlockedUntil > Date.now()) return false; // 刚失败过，等冷却
  return true;
}

/**
 * 请 native host 拉起本地服务。协议见 native_host/linkwork_host.py。
 *
 * 失败要能解释清楚，因为这一步常见的坑是：
 *   1. 没装 native host（未注册 / 未重启浏览器）→ "Specified native messaging host not found"；
 *   2. 扩展 ID 与注册时允许的不一致 → "Access to the specified native messaging host is forbidden"；
 *   3. 本机程序启动了但立刻退出 → "Native host has exited"
 *      （最常见原因是首次运行时进程还没跑到读消息就被回收，重试一次往往就好了；
 *        重试仍失败则要看 ~/Library/Logs/LinkWork-native-host.log）。
 */
async function startServiceViaHost(link) {
  if (link.startInFlight) return { ok: false, error: '正在启动中，请稍候' };
  link.startInFlight = true;
  try {
    let lastError = null;
    for (let attempt = 1; attempt <= NATIVE_HOST_ATTEMPTS; attempt += 1) {
      try {
        const response = await sendNativeMessageWithTimeout({ action: 'start' }, NATIVE_HOST_TIMEOUT_MS);
        if (!response) throw new Error('本机程序没有返回结果');
        if (!response.ok) throw new Error(response.error || '本机程序启动服务失败');
        console.log(`[LinkWork] native host 已启动本地服务（第 ${attempt} 次尝试）：`, JSON.stringify(response));
        return { ok: true, data: response };
      } catch (err) {
        lastError = err;
        const message = describeError(err);
        // 「启动了但立刻退出」多为瞬时故障（首次运行、进程竞态），退避后再给一次机会
        if (attempt < NATIVE_HOST_ATTEMPTS && /exited|has exited|没有返回结果|disconnected/i.test(message)) {
          console.warn(`[LinkWork] native host 第 ${attempt} 次启动失败，准备重试：${message}`);
          await new Promise((resolve) => setTimeout(resolve, nativeHostRetryDelayMs));
          continue;
        }
        throw err;
      }
    }
    throw lastError || new Error('本机程序启动服务失败');
  } catch (err) {
    const message = describeError(err);
    link.startBlockedUntil = Date.now() + AUTOSTART_RETRY_MS;
    const hint = /not found|找不到/.test(message)
      ? '（还没安装本机启动器：在项目目录运行 .venv/bin/python scripts/install_native_host.py，然后重启浏览器）'
      : /forbidden|拒绝|not allowed/.test(message)
        ? '（扩展 ID 与本机启动器注册的不一致：重跑 install_native_host.py 让它读到真实 ID）'
        : /exited/i.test(message)
          ? '（本机启动器启动后立刻退出了；已重试仍失败，请查看 ~/Library/Logs/LinkWork-native-host.log）'
          : '';
    console.warn('[LinkWork] 自动启动本地服务失败：', message);
    return { ok: false, error: `自动启动本地服务失败：${message}${hint}` };
  } finally {
    link.startInFlight = false;
  }
}

/** 带超时的 sendNativeMessage：host 卡住时不能让界面无限等 */
function sendNativeMessageWithTimeout(message, timeoutMs) {
  return Promise.race([
    chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message),
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`本机程序 ${Math.round(timeoutMs / 1000)}s 内没有响应`)), timeoutMs);
    }),
  ]);
}

/** 请 native host 启动服务（供面板按钮使用；没有活动连接时也能用） */
async function startServiceForAnyLink() {
  const link = panelLinks.values().next().value;
  if (link) {
    const result = await startServiceViaHost(link);
    if (!result.ok) throw new Error(result.error);
    await refreshPanelLink(link);
    return result.data || { ok: true };
  }
  // 面板还没建连（少见）：临时造一个只用于启动的上下文
  return startServiceViaHost({ startInFlight: false, startBlockedUntil: 0 })
    .then((result) => {
      if (!result.ok) throw new Error(result.error);
      return result.data || { ok: true };
    });
}

/** 建立一条侧边栏连接：立刻检查（+ 按需拉起服务）+ 起心跳 */
function attachPanelLink(port, sender) {
  const origin = sender || port.sender || null;
  const link = {
    port,
    // 侧边栏是扩展页（不是标签页），windowId 直接挂在 sender 上；
    // 若将来从标签页侧连接，则退回到 sender.tab.windowId。
    windowId: origin?.tab?.windowId ?? origin?.windowId ?? null,
    timer: null,
    inflight: false,
    reported: false,
    lastStatus: '',
    lastText: '',
    startInFlight: false,
    startBlockedUntil: 0,
  };
  panelLinks.set(port, link);
  console.log(`[LinkWork] 侧边栏已打开，建立服务连接（第 ${panelLinks.size} 条）`);
  // 开关先同步（异步），再立刻探活——探活时若服务没跑且开关为开，就会自动拉起
  syncAutoStart().finally(() => refreshPanelLink(link));
  link.timer = setInterval(() => refreshPanelLink(link), PANEL_HEARTBEAT_MS);
  return link;
}

/** 侧边栏关闭/浏览器退出/扩展重载：断开连接，停止探活 */
function detachPanelLink(port) {
  const link = panelLinks.get(port);
  if (!link) return;
  clearInterval(link.timer);
  panelLinks.delete(port);
  console.log(`[LinkWork] 侧边栏已关闭，服务连接断开（剩余 ${panelLinks.size} 条）`);
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PANEL_PORT_NAME) return;
  attachPanelLink(port, port.sender);
  port.onDisconnect.addListener(() => detachPanelLink(port));
});

// ---------------------------------------------------------------- 消息路由
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: describeError(err) }));
  return true; // 异步响应
});

function describeError(err) {
  if (!err) return '未知错误';
  if (typeof err === 'string') return err;
  const parts = [err.message || String(err)];
  if (err.detail) parts.push(err.detail);
  if (err.code) parts.push(`(${err.code})`);
  return parts.join(' ');
}

async function handleMessage(message) {
  switch (message?.type) {
    case 'service:health':
      return serviceRequest('/api/health', 'GET', null, 4000);
    case 'service:start':
      // 面板上的「启动服务」按钮：借任一活动连接走 native host
      return startServiceForAnyLink();
    case 'service:autoStart':
      return setAutoStart(message.enabled);
    case 'service:autoStartState':
      return { enabled: await syncAutoStart() };
    case 'service:resolve':
      return serviceRequest('/api/resolve', 'POST', { url: message.url });
    case 'service:format':
      return serviceRequest('/api/format', 'POST', message.payload);
    case 'service:transcribe':
      return serviceRequest('/api/transcribe', 'POST', message.payload, 120000);
    case 'service:job':
      return serviceRequest(`/api/jobs/${message.jobId}`, 'GET', null, 10000);
    case 'service:cache':
      return serviceRequest('/api/cache', 'GET', null, 8000);
    case 'service:cacheClear':
      return serviceRequest('/api/cache/clear', 'POST', {});
    case 'service:ocr':
      return serviceRequest('/api/ocr', 'POST', message.payload, 120000);
    case 'page:cookies':
      return readPageCookies(message.tabId);
    case 'page:closeTab':
      await closeTabSafely(message.tabId);
      return true;
    case 'page:extract':
      return extractFromTab(message.tabId, message.url);
    case 'page:fetchMedia':
      return fetchMediaInPage(message.tabId, message.mediaUrl, message.maxBytes);
    default:
      throw new Error(`未知消息类型：${message?.type}`);
  }
}

// ---------------------------------------------------------------- 本地服务
async function serviceRequest(path, method, body, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${SERVICE_URL}${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      throw new Error(`本地服务返回了非 JSON 内容（HTTP ${response.status}）：${text.slice(0, 200)}`);
    }
    if (!response.ok) {
      const err = new Error(data?.error || `本地服务返回 HTTP ${response.status}`);
      err.code = data?.code || `http_${response.status}`;
      throw err;
    }
    // 统一契约：同步接口的响应形如 {ok:true, result:{...}}，
    // 这里把 result 解开再交给侧边栏，避免侧边栏还要区分「有没有 result 外层」。
    //
    // 必须排除「任务对象」：已完成任务的响应是
    //   {id, kind, state:"done", stage, progress, result:{...}}
    // 它同样含 result 键，若被解开就会丢掉 state/stage/progress，
    // 侧边栏会认为响应结构异常而永远等不到完成。
    // 判据：带 state（任务）或带 job（异步提交）都保持原样。
    const isJobLike =
      data && typeof data === 'object' && ('state' in data || 'job' in data);
    if (!isJobLike && data && typeof data === 'object' && 'result' in data) {
      return data.result;
    }
    return data;
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error('本地服务响应超时，请确认已运行：linkwork serve');
    }
    if (err instanceof TypeError) {
      // fetch 网络层失败（服务未启动）
      throw new Error('无法连接本地服务（127.0.0.1:17890）。请先在终端运行：linkwork serve');
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------- 页面提取
/**
 * 在页面"主世界"里执行提取脚本，这样能读到站点注入的全局对象
 * （抖音 _ROUTER_DATA / B站 __INITIAL_STATE__ / 小红书 __INITIAL_STATE__）。
 */
/**
 * 提取页面数据。
 *
 * 注意标签页生命周期：视频类页面在返回后还需要"页面内抓媒体字节"，
 * 因此有媒体时**先不关闭**标签页，返回 closeTabId 交给调用方，
 * 由调用方在抓完媒体（或放弃）后调用 closeTab 关闭。
 */
async function extractFromTab(tabId, urlHint) {
  let targetTabId = tabId;
  let createdTabId = null;

  if (!targetTabId) {
    if (!urlHint) throw new Error('缺少要提取的页面：请传入链接或先打开目标页面');
    const tab = await chrome.tabs.create({ url: urlHint, active: false });
    createdTabId = tab.id;
    targetTabId = tab.id;
    await waitForTabComplete(targetTabId, 30000);
    // 抖音这类站点的媒体地址是前端请求后注入的（服务端 HTML 里为空），
    // 因此这里轮询等待"媒体就绪"而不是固定等待一个时长。
    await waitForMediaReady(targetTabId, 8000);
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId: targetTabId },
    world: 'MAIN',
    func: pageExtractor,  // 来自 extractor.js
  });
  const payload = results?.[0]?.result;
  if (!payload) throw new Error('页面提取脚本没有返回数据');

  // 有媒体且是我们自己新建的标签页 → 先留着，等抓完媒体再关
  if (!payload.error && payload.hasMedia && createdTabId) {
    payload.tabId = createdTabId;
    payload.closeTabId = createdTabId;
  } else if (createdTabId) {
    await closeTabSafely(createdTabId);
  }
  payload.tabId = payload.tabId || targetTabId;

  if (payload.error) throw new Error(payload.error);

  // 附带该页面的 cookie，供本地服务下载媒体时使用（等同页面自身请求）
  try {
    payload.cookie = (await readPageCookies(payload.tabId)) || '';
  } catch {
    payload.cookie = '';
  }
  return payload;
}

/**
 * 轮询等待页面出现可用媒体（或超时）。
 * 每次都在页面主世界执行一次轻量探测：<video> 有真实地址，或 RENDER_DATA 里出现媒体 URL。
 */
async function waitForMediaReady(tabId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let attempt = 0;
  while (Date.now() < deadline) {
    attempt += 1;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          const v = document.querySelector('video');
          const src = (v && (v.currentSrc || v.src)) || '';
          if (/^https?:/.test(src) && !src.startsWith('blob:')) return 'video';
          const el = document.getElementById('RENDER_DATA');
          const text = el ? el.textContent || '' : '';
          if (/douyinvod|aweme\/v1\/play|playwm/.test(text)) return 'data';
          return '';
        },
      });
      const hit = results?.[0]?.result;
      if (hit) {
        await sleep(300); // 让播放器再稳定一下
        return hit;
      }
    } catch {
      /* 页面可能还在导航，忽略后继续等 */
    }
    await sleep(Math.min(400 + attempt * 200, 1500));
  }
  return '';
}

async function closeTabSafely(tabId) {
  if (!tabId) return;
  try {
    await chrome.tabs.remove(tabId);
  } catch {
    /* 忽略：标签页可能已被用户关闭 */
  }
}

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === 'complete') finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    const timer = setTimeout(finish, timeoutMs);
    // 可能已经加载完成
    chrome.tabs.get(tabId).then((tab) => {
      if (tab?.status === 'complete') finish();
    }).catch(() => finish());
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 读取某个标签页当前域名下的 cookie（拼成 Cookie 头） */
async function readPageCookies(tabId) {
  if (!tabId) return '';
  const tab = await chrome.tabs.get(tabId);
  if (!tab?.url || !/^https?:/.test(tab.url)) return '';
  const cookies = await chrome.cookies.getAll({ url: tab.url });
  if (!cookies.length) return '';
  return cookies.map((c) => `${c.name}=${c.value}`).join('; ');
}

/**
 * 在页面上下文中抓取媒体字节（带上页面的 Cookie 与 Referer，等同于页面自身的请求）。
 * 返回 base64；超过 maxBytes 时抛错，由调用方改用 URL 直传方案。
 */
async function fetchMediaInPage(tabId, mediaUrl, maxBytes) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    args: [mediaUrl, maxBytes],
    func: async (url, limit) => {
      try {
        const response = await fetch(url, { credentials: 'include' });
        if (!response.ok) {
          return { error: `媒体请求失败：HTTP ${response.status}` };
        }
        const total = Number(response.headers.get('Content-Length') || 0);
        if (total && total > limit) {
          return { tooLarge: true, size: total };
        }
        const buffer = await response.arrayBuffer();
        if (buffer.byteLength > limit) {
          return { tooLarge: true, size: buffer.byteLength };
        }
        let binary = '';
        const bytes = new Uint8Array(buffer);
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        return {
          base64: btoa(binary),
          size: buffer.byteLength,
          contentType: response.headers.get('Content-Type') || '',
        };
      } catch (err) {
        return { error: `页面内抓取媒体失败：${err?.message || err}` };
      }
    },
  });
  const result = results?.[0]?.result;
  if (!result) throw new Error('页面内抓取媒体没有返回结果');
  if (result.error) throw new Error(result.error);
  if (result.tooLarge) {
    const err = new Error(`媒体体积过大（${Math.round(result.size / 1048576)}MB），改用直链方式传输`);
    err.tooLarge = true;
    err.size = result.size;
    throw err;
  }
  return result;
}

/**
 * ⚠️ 这个函数会被序列化后在页面主世界执行，不能引用外部变量或闭包。
 * 它是整个扩展的"取数入口"：读取平台注入的数据、DOM 文案、媒体地址。
 */

/**
 * 测试钩子：把连接生命周期的内部对象暴露出来，供 tests/background_boot.test.js
 * 直接驱动（MV3 里没有别的办法观察 service worker 内部状态）。
 * 只读不写，正式运行时不参与任何逻辑。
 */
globalThis.__linkworkPanelBridge = {
  PANEL_PORT_NAME,
  PANEL_HEARTBEAT_MS,
  NATIVE_HOST_NAME,
  AUTOSTART_RETRY_MS,
  NATIVE_HOST_TIMEOUT_MS,
  NATIVE_HOST_ATTEMPTS,
  setNativeHostRetryDelay: (ms) => { nativeHostRetryDelayMs = ms; },
  panelLinks,
  attachPanelLink,
  detachPanelLink,
  refreshPanelLink,
  describeHealth,
  syncAutoStart,
  setAutoStart,
  startServiceViaHost,
  isAutoStartEnabled: () => autoStartService,
};

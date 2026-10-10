/**
 * LinkWork 扩展：service worker。
 *
 * 职责：
 *  - 点击工具栏图标 → 打开右侧边栏
 *  - 与本地 Python 服务通信（健康检查、提交任务、轮询结果）
 *  - 读取页面数据：通过 chrome.scripting 在页面主世界（MAIN world）执行提取函数
 *
 * 设计原则：失败就报错，不做降级猜测。
 */

importScripts('extractor.js');

const SERVICE_URL = 'http://127.0.0.1:17890';

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

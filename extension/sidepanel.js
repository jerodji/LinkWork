/**
 * LinkWork 侧边栏逻辑。
 *
 * 流程：
 *   输入链接 → 判断类型
 *     ├─ 视频类：后台标签页提取媒体 → 交给本地服务做语音识别 → 显示口播稿
 *     ├─ 图文类：后台标签页提取正文/图片 → 本地服务整理 / OCR → 显示文案
 *     └─ 纯文本：直接交给本地服务整理
 *   失败即报错，不做降级猜测。
 */

const SERVICE_REQUIRED_HINT = '请先在终端运行：linkwork serve';

const $ = (id) => document.getElementById(id);
const els = {
  urlInput: $('urlInput'),
  extractBtn: $('extractBtn'),
  clearBtn: $('clearBtn'),
  resultBox: $('resultBox'),
  resultMeta: $('resultMeta'),
  copyBtn: $('copyBtn'),
  copyRawBtn: $('copyRawBtn'),
  clearResultBtn: $('clearResultBtn'),
  statusDot: $('statusDot'),
  statusText: $('statusText'),
  connState: $('connState'),
  modeHint: $('modeHint'),
  progressWrap: $('progressWrap'),
  progressBar: $('progressBar'),
  logBox: $('logBox'),
  logCount: $('logCount'),
  toast: $('toast'),
  copyLogBtn: $('copyLogBtn'),
  clearLogBtn: $('clearLogBtn'),
  cacheSize: $('cacheSize'),
  clearCacheBtn: $('clearCacheBtn'),
  autoClear: $('autoClear'),
  autoStartService: $('autoStartService'),
  startServiceBtn: $('startServiceBtn'),
  installHostBtn: $('installHostBtn'),
  startHint: $('startHint'),
};

let logCount = 0;
let lastRawText = '';
let busy = false;
/** 到扩展后台的长连接：非空即代表「连接已开启」 */
let port = null;
/** 连接断开后的自动重连定时器 */
let reconnectTimer = null;

// ------------------------------------------------------------------ 工具
function log(line) {
  logCount += 1;
  els.logCount.textContent = String(logCount);
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  els.logBox.textContent += `[${time}] ${line}\n`;
  els.logBox.scrollTop = els.logBox.scrollHeight;
}

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.className = `toast show${isError ? ' err' : ''}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    els.toast.className = 'toast';
  }, 2600);
}

function setHint(text, kind = '') {
  els.modeHint.textContent = text;
  els.modeHint.className = `hint${kind ? ' ' + kind : ''}`;
}

function setStatus(state, text) {
  els.statusDot.className = `dot ${state}`;
  els.statusText.textContent = text;
}

// 各阶段在总进度中的区间（与抓包观察到的耗时占比一致）
const STAGE_RANGES = [
  ['准备媒体', 0.0, 0.02],
  ['下载媒体', 0.02, 0.30],
  ['下载完成', 0.30, 0.44],
  ['提取音频', 0.44, 0.56],
  ['语音识别', 0.58, 1.0],
];

/** 把「阶段 + 该阶段内的进度」映射成总进度并渲染 */
function setProgress(stage, ratio) {
  const range = STAGE_RANGES.find(([name]) => stage && stage.includes(name));
  if (!range) return; // 未知阶段不推进，保持当前值
  const [, lo, hi] = range;
  const clamped = Math.max(0, Math.min(Number(ratio) || 0, 1));
  const pct = Math.round((lo + (hi - lo) * clamped) * 100);
  els.progressWrap.classList.add('active');
  els.progressBar.style.width = `${pct}%`;
}

/** 提取结束后把进度条置满并保持可见（需求：展示出文案时进度为 100%） */
function finishProgress() {
  els.progressWrap.classList.add('active');
  els.progressBar.style.width = '100%';
}

function setBusy(value) {
  busy = value;
  els.extractBtn.disabled = value;
  els.clearBtn.disabled = value;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 从文本中抠出第一个 http(s) 链接 */
function extractUrl(text) {
  const match = (text || '').match(/https?:\/\/[^\s，。、）)】\]"'<>]+/);
  if (!match) return '';
  return match[0].replace(/[，。、；,.;)）】]+$/, '');
}

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return '';
  }
}

const VIDEO_HOSTS = ['douyin.com', 'iesdouyin.com', 'bilibili.com', 'b23.tv', 'xiaohongshu.com', 'xhslink.com', 'youtube.com', 'youtu.be', 'kuaishou.com'];

function platformHint(url) {
  const host = hostOf(url);
  if (/douyin|iesdouyin/.test(host)) return '抖音';
  if (/bilibili|b23\.tv/.test(host)) return 'B站';
  if (/xiaohongshu|xhslink/.test(host)) return '小红书';
  if (/youtube|youtu\.be/.test(host)) return 'YouTube';
  if (/kuaishou/.test(host)) return '快手';
  if (/mp\.weixin\.qq\.com/.test(host)) return '微信公众号';
  return host || '网页';
}

/**
 * 统一的后台消息调用。
 *
 * 必须带超时：chrome.runtime.sendMessage 在 service worker 被浏览器回收时
 * 可能永远不 resolve，若不加保护，界面就会永久停在最后一条进度上
 * （曾出现过后端已成功、侧边栏仍显示「处理中 0%」）。
 */
async function call(type, payload = {}, timeoutMs = 60000) {
  let timer = null;
  try {
    const response = await Promise.race([
      chrome.runtime.sendMessage({ type, ...payload }),
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error('扩展后台无响应（可能被浏览器回收），请重试；若反复出现请重新加载扩展')),
          timeoutMs,
        );
      }),
    ]);
    if (!response) throw new Error('扩展后台没有响应（service worker 可能被回收，请重试）');
    if (!response.ok) throw new Error(response.error || '调用失败');
    return response.data;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ------------------------------------------------------------------ 服务状态
/**
 * 连接状态语义（由后台推送，见 connectPanel）：
 *   ok          —— 已连上本地服务，后台正在按 PANEL_HEARTBEAT_MS 心跳探活
 *   checking    —— 正在检查
 *   fail        —— 连不上本地服务（后台持续自动重试）
 *   disconnected—— 与扩展后台的连接断了（后台被回收 / 扩展被重载）
 */
let linkState = 'checking';
let linkEverConnected = false;

/** 重连入口：连接断开或后台未就绪时点击状态灯可手动重连（正常情况下会自动重连） */
function reconnectPanel() {
  if (port || busy) return; // 已有连接，或正在提取，等它自己恢复
  log('正在重新连接本地服务…');
  connectPanel();
}

/**
 * 侧边栏打开即建立到扩展后台的长连接。
 *
 * 这条连接就是「插件开着 = 服务连接开着」的载体：
 *   - 打开：connect → 后台立刻做健康检查，并每 20s 心跳探活；
 *   - 关闭侧边栏 / 关闭浏览器：浏览器自动断开该连接，后台随之清理，不再探活。
 *
 * 注意：服务进程本身是常驻的（`linkwork serve`），这里开关的是「连接」，
 * 不会去启动或停止那个进程。
 */
function connectPanel() {
  if (port) return;

  setStatus('busy', '连接服务…');
  setConnState('connecting');
  let created;
  try {
    created = chrome.runtime.connect({ name: 'linkwork-panel' });
  } catch (err) {
    // 后台暂不可用（扩展刚被重载等）：留到下一个重连周期再试
    linkState = 'disconnected';
    setStatus('err', '后台未就绪，自动重连中');
    setConnState('off');
    log(`建立连接失败：${err?.message || err}`);
    scheduleReconnect();
    return;
  }
  port = created;

  created.onMessage.addListener((message) => {
    if (message?.type !== 'service:state') return;
    applyServiceState(message);
  });

  // 浏览器关闭、侧边栏关闭、扩展重载、后台被回收都会走到这里
  created.onDisconnect.addListener(() => {
    if (port !== created) return; // 已被更新的连接取代
    port = null;
    linkState = 'disconnected';
    if (busy) {
      // 正在提取时断开：重新连上，任务轮询才能继续
      setStatus('busy', '连接中断，正在重连…');
      setConnState('off');
      log('与扩展后台的连接中断（后台可能被回收），正在自动重连…');
    } else {
      setStatus('err', '连接已断开（点此重连）');
      setConnState('off');
      log('与扩展后台的连接已断开（后台被回收或扩展已重载）');
    }
    scheduleReconnect();
  });
}

/** 连接断开后的自动重连；重连成功即恢复状态灯与心跳 */
function scheduleReconnect() {
  clearInterval(reconnectTimer);
  reconnectTimer = setInterval(() => {
    if (port || busy) return; // 提取过程中不要打断现有流程
    connectPanel();
  }, 3000);
}

/** 把后台推送的连接状态落到界面上 */
function applyServiceState(state) {
  if (state.status === 'ok') {
    if (!linkEverConnected) {
      linkEverConnected = true;
      const asr = (state.asr || []).find((e) => e.available);
      log(`已连接本地服务（v${state.version || '?'}，ASR：${asr ? asr.name : '无'}），关掉侧边栏即自动断开`);
    }
  } else if (state.status !== 'checking' && linkState !== 'fail') {
    log(`本地服务无法连接：${state.title || state.text || ''}`);
  }
  // 提取过程中右上角显示的是阶段进度，别被连接状态覆盖掉
  if (!busy) {
    if (state.status === 'ok') setStatus('ok', state.text || '已连接');
    else if (state.status === 'checking') setStatus('busy', state.text || '检查服务…');
    else setStatus('err', state.text || '服务未连接');
  }
  setConnState(state.status === 'ok' ? 'on' : 'off');
  // 启动区提示：服务没连上时告诉用户「点按钮」或「本机启动器没装」
  if (state.status === 'checking') {
    els.startHint.textContent = '正在启动本地服务…';
    els.startHint.className = 'hint';
  } else if (state.status === 'ok') {
    els.startHint.textContent = readAutoStartPref()
      ? '打开侧边栏时若服务没跑，会自动启动。'
      : '已连接。自动启动当前是关闭的。';
    els.startHint.className = 'hint';
  } else if (readAutoStartPref()) {
    els.startHint.textContent = '自动启动失败或本机启动器未安装，可点「启动服务」重试。';
    els.startHint.className = 'hint error';
  } else {
    els.startHint.textContent = '服务未运行。点「启动服务」立即启动，或勾选「打开时自动启动」。';
    els.startHint.className = 'hint';
  }
  const box = els.statusText.parentElement;
  if (box) {
    box.title = state.title || '';
    box.classList.toggle('clickable', state.status === 'fail' || state.status === 'disconnected');
  }
  linkState = state.status;
}

// ------------------------------------------------------------------ 启动本地服务
/**
 * 「服务没跑」时的处理：
 *   - 打开面板时若勾了「打开时自动启动」，后台会自动通过 native host 拉起服务；
 *   - 也可以随时点「启动服务」按钮手动拉起。
 *
 * 说明：浏览器扩展没有权限执行 shell 命令，真正执行的是已注册的 native host
 * （native_host/linkwork_host.py，由 scripts/install_native_host.py 注册）。
 */
const AUTO_START_KEY = 'lw_auto_start_service';
const INSTALL_HINT = '本会话无法启动服务：需要在项目目录运行 '
  + '.venv/bin/python scripts/install_native_host.py 注册本机启动器，然后重启浏览器。';

function readAutoStartPref() {
  return localStorage.getItem(AUTO_START_KEY) !== '0'; // 默认开启
}

/** 把开关同步给后台（后台决定探活失败时要不要拉起服务） */
async function syncAutoStartPref(quiet = true) {
  const enabled = readAutoStartPref();
  els.autoStartService.checked = enabled;
  try {
    await call('service:autoStart', { enabled }, 8000);
  } catch (err) {
    if (!quiet) log(`同步自动启动开关失败：${err.message}`);
  }
  return enabled;
}

/** 手动「启动服务」：请后台通过 native host 拉起，然后立刻重新探活 */
async function startServiceManually() {
  if (busy) return;
  els.startServiceBtn.disabled = true;
  setHint('正在启动本地服务…');
  setStatus('busy', '正在启动服务…');
  log('请求启动本地服务（通过本机启动器）…');
  try {
    const result = await call('service:start', {}, 90000);
    log(`启动结果：${JSON.stringify(result)}`);
    if (result?.alreadyRunning) {
      log('服务本来就在运行（如果提取报 Operation not permitted，说明它是被受限环境启动的，需要先停掉它再启动）');
    } else if (result?.pid) {
      log(`服务已启动（PID ${result.pid}${result.cacheDir ? `，缓存 ${result.cacheDir}` : ''}）`);
    }
    if (result?.cacheNote) {
      // 默认缓存目录不可写时 host 会换一个可写目录，这里必须告诉用户
      els.startHint.textContent = result.cacheNote;
      els.startHint.className = 'hint error';
      log(`⚠️ ${result.cacheNote}`);
      toast('已换用可写的缓存目录，详见日志', true);
    }
    await refreshCache();
  } catch (err) {
    const message = err.message || String(err);
    log(`❌ 启动服务失败：${message}`);
    els.startHint.textContent = message.includes('本机程序') || message.includes('not found') ? INSTALL_HINT : message;
    els.startHint.className = 'hint error';
    toast('启动服务失败，详见日志', true);
  } finally {
    els.startServiceBtn.disabled = false;
  }
}

/** 右上角的「连接」开关状态：侧边栏开着就一直亮着，关掉侧边栏随之消失 */
function setConnState(state) {
  // 标题里也带上连接状态：浏览器标签/面板标题一眼可见，也便于外部自检读取
  const suffix = state === 'on' ? '连接已开启' : state === 'off' ? '服务未连接' : '连接中';
  document.title = `LinkWork 文案提取 · ${suffix}`;
  if (!els.connState) return;
  els.connState.classList.remove('on', 'off');
  if (state === 'on') {
    els.connState.classList.add('on');
    els.connState.textContent = '连接已开启';
    els.connState.title = '侧边栏打开期间保持与本地服务的连接；关闭侧边栏或退出浏览器即自动断开';
  } else if (state === 'off') {
    els.connState.classList.add('off');
    els.connState.textContent = '服务未连接';
    els.connState.title = '连不上本地服务。请确认已运行：linkwork serve';
  } else {
    els.connState.textContent = '连接中';
    els.connState.title = '正在建立与本地服务的连接';
  }
}

// ------------------------------------------------------------------ 任务轮询
async function waitJob(jobId, label) {
  const deadline = Date.now() + 10 * 60 * 1000;
  let lastStage = '';
  let consecutiveErrors = 0;
  let polls = 0;
  log(`开始轮询任务 ${jobId}`);
  while (Date.now() < deadline) {
    polls += 1;
    let job;
    try {
      // 单次查询给 20s：后端在此期间必定能答复（本地回环）
      job = await call('service:job', { jobId }, 20000);
      consecutiveErrors = 0;
    } catch (err) {
      consecutiveErrors += 1;
      log(`轮询失败（第 ${consecutiveErrors} 次）：${err.message}`);
      if (consecutiveErrors >= 5) {
        const hint = /job_not_found|任务不存在/.test(err.message)
          ? '本地服务可能在任务执行期间重启过（任务记录保存在内存里）。请重试一次。'
          : '';
        throw new Error(`连续 ${consecutiveErrors} 次无法查询任务状态：${err.message}${hint ? '。' + hint : ''}`);
      }
      await sleep(1500);
      continue;
    }
    if (!job || typeof job !== 'object' || !('state' in job)) {
      // 典型场景：扩展被重载/后台被回收时，sendMessage 会返回 undefined。
      // 这属于瞬时故障，重试即可，不能直接崩掉整个等待流程。
      consecutiveErrors += 1;
      let preview;
      try {
        preview = JSON.stringify(job);
      } catch {
        preview = String(job);
      }
      log(`响应结构异常（第 ${consecutiveErrors} 次，无 state 字段）：${String(preview).slice(0, 200)}`);
      if (consecutiveErrors >= 5) {
        throw new Error(
          `连续 ${consecutiveErrors} 次拿到异常响应（可能是扩展被重新加载）。请在 edge://extensions 里重新加载扩展后重试。`,
        );
      }
      await sleep(1500);
      continue;
    }
    // 只要已带回 result 就直接用，不必再等 state 确认。
    // 否则「错过一次 done」+ 服务重启（任务表在内存中）就会永远拿不回结果。
    if (job.result) {
      log(`${label} 完成（轮询 ${polls} 次）`);
      return job.result;
    }
    if (job.state === 'done') {
      log(`${label} 完成（轮询 ${polls} 次）`);
      return job.result;
    }
    if (job.state === 'error') {
      throw new Error(job.error || '任务失败');
    }
    const stage = `${job.stage || '处理中'} ${Math.round((job.progress || 0) * 100)}%`;
    if (stage !== lastStage) {
      lastStage = stage;
      setHint(`${label}：${stage}`);
      setProgress(job.stage, job.progress);
      // 每次状态变化都带上原始 state，便于排查"停在某处不动"
      log(`${label}：${stage}（state=${job.state || '?'}，轮询第 ${polls} 次）`);
    }
    if (polls % 15 === 0) {
      log(`…仍在等待（已轮询 ${polls} 次，当前 ${stage}）`);
    }
    await sleep(1200);
  }
  throw new Error('任务超时（超过 10 分钟），请检查本地服务日志');
}

// ------------------------------------------------------------------ 主流程
/**
 * 提取前的连接自检。
 * 面板打开时后台已经连好并每 20s 探活，这里只是提取前再确认一次，
 * 顺便把界面状态刷新到与服务一致（服务中途重启也不会误报）。
 */
async function ensureServiceReady() {
  try {
    const health = await call('service:health');
    const asr = (health.asr || []).find((e) => e.available);
    setStatus('ok', `已连接：${health.name || 'LinkWork 本地服务'}${asr ? ` · ${asr.name}` : ''}`);
    linkState = 'ok';
    return health;
  } catch (err) {
    setStatus('err', '服务未连接');
    linkState = 'fail';
    throw new Error(`本地服务未启动。${SERVICE_REQUIRED_HINT}（${err.message}）`);
  }
}

async function runExtract() {
  if (busy) return;

  const raw = els.urlInput.value.trim();
  if (!raw) throw new Error('请先粘贴链接或文案');
  const input = extractUrl(raw) || raw;
  log(`开始提取：${input}`);

  const isUrl = /^https?:\/\//.test(input);
  setBusy(true);
  setHint('准备中…');
  els.progressWrap.classList.add('active');
  els.progressBar.style.width = '3%';
  els.resultBox.value = '';
  els.resultMeta.textContent = '';

  try {
    await ensureServiceReady();

    if (!isUrl) {
      log('输入不是链接，按纯文本整理');
      const result = await call('service:format', {
        payload: { text: input, title: '', kind: 'text' },
      });
      showResult(result);
      return;
    }

    const platform = platformHint(input);
    log(`识别平台：${platform}`);

    // 1) 打开（或复用）页面并提取数据
    setHint('正在读取页面数据…');
    const pageData = await call('page:extract', { url: input });
    logSources(pageData);
    log(
      `页面信息：平台=${pageData.platform || platform}，媒体=${pageData.hasMedia ? '有' : '无'}，`
      + `文案=${(pageData.desc || pageData.text || '').length} 字，图片=${pageData.images?.length || 0}`,
    );
    const preview = (pageData.desc || pageData.text || '').replace(/\s+/g, ' ').slice(0, 50);
    if (preview) log(`文案预览: ${preview}${preview.length >= 50 ? '…' : ''}`);
    if (pageData.error) throw new Error(pageData.error);

    // 2) B 站有平台字幕，优先走服务端解析（比语音识别更准更快）
    if (pageData.bvid) {
      setHint('正在解析 B 站视频（优先平台字幕）…');
      log(`B 站视频号：${pageData.bvid}`);
      const job = await call('service:resolve', { url: `https://www.bilibili.com/video/${pageData.bvid}` });
      const result = await waitJob(job.job.id, 'B站解析');
      showResult(result);
      return;
    }

    // 3) 有媒体 → 走语音识别
    if (pageData.hasMedia) {
      const result = await transcribeMedia(pageData);
      showResult(result);
      return;
    }

    // 4) 有图片（图文笔记）→ 走 OCR
    if (pageData.images?.length) {
      setHint('正在识别图片文字…');
      const job = await call('service:ocr', {
        payload: {
          images: pageData.images,
          title: pageData.title,
          author: pageData.author,
          url: pageData.url,
          page_url: pageData.url,
          cookie: pageData.cookie || '',
        },
      });
      const result = await waitJob(job.job.id, '图片文字识别');
      showResult(result);
      return;
    }

    // 到这里说明页面上既没有媒体也没有图片：要么有纯文本，要么明确取不到
    const plainText = (pageData.text || pageData.desc || '').trim();
    if (!plainText) {
      // 不要退化成 pageData.error —— 那是报错信息，不是文案
      throw new Error(pageData.error || '没有从该链接提取到文案或媒体');
    }

    // 5) 纯文本页面 → 直接整理
    setHint('正在整理页面文案…');
    const result = await call('service:format', {
      payload: {
        text: plainText,
        title: pageData.title,
        author: pageData.author,
        url: pageData.url,
        kind: pageData.kind,
        platform: pageData.platform,
      },
    });
    showResult(result);
  } catch (err) {
    const message = err?.message || String(err);
    log(`❌ ${message}`);
    setHint(message, 'error');
    toast(message, true);
    // 失败时也要让进度条收尾，否则会一直挂在半途
    els.progressWrap.classList.remove('active');
    els.progressBar.style.width = '0';
    els.resultBox.value = '';
    els.resultMeta.textContent = '';
  } finally {
    setBusy(false);
  }
}

/**
 * 媒体 → 语音识别。
 * 优先用音频流（抖音 music.play_url，体积远小于视频），
 * 其次视频流；优先在页面内抓字节（自带页面 Cookie），体积过大则改用直链。
 */
async function transcribeMedia(pageData) {
  const audioUrl = pageData.audioUrl || pageData.audioCandidates?.[0] || '';
  const videoUrl = pageData.mediaUrl || pageData.mediaCandidates?.[0] || '';
  const mediaUrl = audioUrl || videoUrl;
  if (!mediaUrl) {
    throw new Error('页面中未找到可下载的媒体地址（可能视频未开始播放，请点击播放后重试）');
  }
  if (audioUrl) log('已找到音频流地址，将只下载音频');

  const MAX_BYTES = 40 * 1024 * 1024;
  try {
    let payload = null;

    // 页面内抓取：与页面同源同 Cookie，最贴近真实播放行为
    if (pageData.tabId) {
      try {
        setHint('正在读取媒体数据…');
        log('尝试在页面内抓取媒体字节…');
        const media = await call('page:fetchMedia', {
          tabId: pageData.tabId,
          mediaUrl,
          maxBytes: MAX_BYTES,
        });
        payload = {
          data_base64: media.base64,
          suffix: guessSuffix(media.contentType, mediaUrl),
          page_url: pageData.url,
          title: pageData.title || pageData.desc || '',
        };
        log(`已获取媒体数据：${(media.size / 1048576).toFixed(1)}MB`);
      } catch (err) {
        if (err.message?.includes('体积过大')) {
          log(`${err.message}，改用直链方式`);
        } else {
          log(`页面内抓取失败：${err.message}（改用直链方式）`);
        }
      }
    }

    if (!payload) {
      setHint('正在下载媒体…');
      log('使用媒体直链交给本地服务下载');
      payload = {
        media_url: mediaUrl,
        page_url: pageData.url,
        title: pageData.title || pageData.desc || '',
        user_agent: navigator.userAgent,
        cookie: pageData.cookie || '',
      };
    }

    const job = await call('service:transcribe', { payload });
    return await waitJob(job.job.id, '语音识别');
  } finally {
    // 后台标签页用完即关，避免留下看不见的窗口
    if (pageData.closeTabId) {
      call('page:closeTab', { tabId: pageData.closeTabId }).catch(() => {});
    }
  }
}

function guessSuffix(contentType, url) {
  const type = (contentType || '').toLowerCase();
  if (type.includes('audio/mp4') || type.includes('audio/m4a')) return '.m4a';
  if (type.includes('audio/mpeg')) return '.mp3';
  if (type.includes('audio/')) return '.m4a';
  if (type.includes('webm')) return '.webm';
  const match = url.split('?')[0].match(/\.(mp4|m4a|mp3|webm|mov)$/i);
  return match ? match[0].toLowerCase() : '.mp4';
}

function showResult(result) {
  const text = (result?.text || '').trim();
  if (!text) {
    throw new Error('没有提取到任何文案内容');
  }
  els.resultBox.value = text;
  lastRawText = text;
  const bits = [];
  if (result.platform) bits.push(result.platform);
  if (result.method) bits.push(methodLabel(result.method));
  if (result.char_count) bits.push(`${result.char_count} 字`);
  if (result.segment_count) bits.push(`${result.segment_count} 段`);
  if (result.duration) bits.push(formatDuration(result.duration));
  els.resultMeta.textContent = bits.join(' · ');
  setHint(`提取完成（${bits.join('，')}）`, 'ok');
  finishProgress();
  log(`✅ 提取完成，共 ${text.length} 字符`);
  if (result.warnings?.length) {
    result.warnings.forEach((w) => log(`⚠️ ${w}`));
  }
  autoClearIfNeeded().catch(() => {});
}

function methodLabel(method) {
  return {
    asr: '语音识别',
    subtitle: '平台字幕',
    html: '网页正文',
    ocr: '图片OCR',
    metadata: '页面文案',
    manual: '整理',
  }[method] || method;
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ------------------------------------------------------------------ 取数来源日志
/** 把取数来源命中情况格式化成一行，便于改版时快速定位 */
function logSources(pageData) {
  const src = pageData.sources || {};
  const mark = (v) => (v === 2 ? '✅' : v === 1 ? '⚠️部分' : '❌');
  const parts = [
    `_ROUTER_DATA ${mark(src.routerData)}`,
    `RENDER_DATA ${mark(src.renderData)}`,
    `DOM选择器 ${mark(src.domSelectors)}`,
    `<video> ${mark(src.domVideo)}`,
  ];
  if (src.genericScan) parts.push('通用媒体扫描 ✅');
  log(`取数来源: ${parts.join(' / ')}（取数逻辑版本 build=${pageData.build ?? '未知'}）`);
  if (pageData.mediaVia) log(`媒体来源: ${pageData.mediaVia}`);
  const diag = pageData.diagnostics || {};
  if (diag.blocks?.length) log(`页面数据块: ${diag.blocks.join(', ')}`);
  if (diag.e2e?.length) log(`data-e2e 取值: ${diag.e2e.join(', ')}`);
  else if (pageData.platform) log('data-e2e 取值: （无）');
}

// ------------------------------------------------------------------ 媒体缓存
const AUTO_CLEAR_KEY = 'linkwork.autoClearMedia';

async function refreshCache() {
  try {
    const data = await call('service:cache');
    const cache = data?.cache || {};
    els.cacheSize.textContent = `${cache.human || '0 B'}${cache.workspaces ? ` · ${cache.workspaces} 个目录` : ''}`;
    els.cacheSize.title = cache.root ? `目录：${cache.root}` : '';
  } catch {
    els.cacheSize.textContent = '—';
  }
}

async function clearCache(quiet = false) {
  try {
    const data = await call('service:cacheClear');
    const cleared = data?.cleared || {};
    log(`已清除媒体缓存：${cleared.removed || 0} 个目录，释放 ${cleared.freed_human || '0 B'}`);
    if (!quiet) toast(`已清除，释放 ${cleared.freed_human || '0 B'}`);
    await refreshCache();
  } catch (err) {
    if (!quiet) toast(`清除失败：${err.message}`, true);
    log(`清除缓存失败：${err.message}`);
  }
}

/** 提取成功后按需自动清理本次下载的媒体 */
async function autoClearIfNeeded() {
  if (!els.autoClear.checked) return;
  await clearCache(true);
}

// ------------------------------------------------------------------ 复制
async function copyText(text, label) {
  if (!text) {
    toast('没有可复制的内容', true);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label}已复制`);
    log(`已复制${label}（${text.length} 字符）`);
  } catch (err) {
    // 剪贴板 API 在侧边栏可能被限制，退回到 textarea 选择复制
    els.resultBox.focus();
    els.resultBox.select();
    const ok = document.execCommand('copy');
    if (ok) {
      toast(`${label}已复制`);
      log(`已复制${label}（${text.length} 字符）`);
    } else {
      toast('复制失败，请手动选择文本复制', true);
      log(`复制失败：${err.message}`);
    }
  }
}

// ------------------------------------------------------------------ 事件绑定
els.extractBtn.addEventListener('click', () => runExtract().catch((e) => toast(e.message, true)));
els.clearBtn.addEventListener('click', () => {
  els.urlInput.value = '';
  els.urlInput.focus();
});

els.copyBtn.addEventListener('click', () => copyText(els.resultBox.value.trim(), '文案'));
els.copyRawBtn.addEventListener('click', () => copyText(lastRawText || els.resultBox.value.trim(), '原文'));
els.copyLogBtn.addEventListener('click', () => {
  copyText(els.logBox.textContent.trim(), '日志');
});
els.clearLogBtn.addEventListener('click', () => {
  els.logBox.textContent = '';
  logCount = 0;
  els.logCount.textContent = '0';
});

els.clearCacheBtn.addEventListener('click', () => {
  clearCache().catch((e) => toast(e.message, true));
});
els.autoClear.addEventListener('change', () => {
  localStorage.setItem(AUTO_CLEAR_KEY, els.autoClear.checked ? '1' : '0');
  log(els.autoClear.checked ? '已开启：每次提取后自动清理媒体' : '已关闭自动清理');
});

els.clearResultBtn.addEventListener('click', () => {
  els.resultBox.value = '';
  els.resultMeta.textContent = '';
  lastRawText = '';
  setHint('');
});

// 连接离线时点状态灯即手动重连（正常情况下由后台自动重连，无需用户操作）
els.statusText.parentElement.addEventListener('click', reconnectPanel);

// 手动启动本地服务
els.startServiceBtn.addEventListener('click', () => {
  startServiceManually().catch((e) => toast(e.message, true));
});
els.installHostBtn.addEventListener('click', () => {
  els.startHint.textContent = INSTALL_HINT;
  els.startHint.className = 'hint';
  log(INSTALL_HINT);
  copyText('.venv/bin/python scripts/install_native_host.py', '本机启动器安装命令');
});
els.autoStartService.addEventListener('change', async () => {
  localStorage.setItem(AUTO_START_KEY, els.autoStartService.checked ? '1' : '0');
  await syncAutoStartPref(false);
  log(els.autoStartService.checked ? '已开启：打开侧边栏时自动启动本地服务' : '已关闭：打开侧边栏时不自动启动服务');
});

// 侧边栏关闭 → 断开连接（浏览器关闭同样会断开；这里只是让断开更及时）
window.addEventListener('beforeunload', () => {
  clearInterval(reconnectTimer);
  try {
    port?.disconnect();
  } catch {
    /* 浏览器正在关闭，忽略 */
  }
  port = null;
});

// Ctrl/Cmd + Enter 快速提取
els.urlInput.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    runExtract().catch((e) => toast(e.message, true));
  }
});

// 初始化：打开侧边栏 = 打开服务连接（必要时自动启动本地服务）
(async () => {
  setHint('把视频 / 图文 / 网页链接粘贴到上方，点击「提取文案」。');
  log('侧边栏已打开，正在连接本地服务…');
  // 恢复偏好（默认开启，避免悄悄占空间）
  els.autoClear.checked = localStorage.getItem(AUTO_CLEAR_KEY) !== '0';
  // 先把「自动启动」开关同步给后台，再建连：连不上时后台会顺手把服务拉起来
  await syncAutoStartPref();
  // 连接本身由后台负责探活；连上后会自动把状态推到界面
  connectPanel();
  await refreshCache();
  els.urlInput.focus();
})();

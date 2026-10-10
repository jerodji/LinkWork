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
};

let logCount = 0;
let lastRawText = '';
let busy = false;

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
async function refreshHealth(quiet = false) {
  setStatus('busy', '检查服务…');
  try {
    const health = await call('service:health');
    const asr = (health.asr || []).find((e) => e.available);
    setStatus('ok', `服务已就绪${asr ? ` · ${asr.name}` : ''}`);
    if (!quiet) log(`本地服务正常（v${health.version}，ASR：${asr ? asr.name : '无'}）`);
    return health;
  } catch (err) {
    setStatus('err', '服务未连接');
    if (!quiet) log(`服务检查失败：${err.message}`);
    return null;
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
    const health = await refreshHealth(true);
    if (!health) throw new Error(`本地服务未启动。${SERVICE_REQUIRED_HINT}`);

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

// Ctrl/Cmd + Enter 快速提取
els.urlInput.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    runExtract().catch((e) => toast(e.message, true));
  }
});

// 初始化
(async () => {
  setHint('把视频 / 图文 / 网页链接粘贴到上方，点击「提取文案」。');
  log('侧边栏已打开');
  // 恢复自动清理偏好（默认开启，避免悄悄占空间）
  els.autoClear.checked = localStorage.getItem(AUTO_CLEAR_KEY) !== '0';
  await refreshHealth();
  await refreshCache();
  els.urlInput.focus();
})();

/** LinkWork 设置页：智谱 API Key、GLM 模型与媒体缓存配置。 */

const SETTINGS_KEY = 'linkwork.settings';
const AUTO_CLEAR_KEY = 'linkwork.autoClearMedia';
const DEFAULT_SETTINGS = { apiKey: '', model: 'glm-4.5-flash', calibrate: true };
const API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions';

const els = {
  apiKey: document.getElementById('apiKey'),
  toggleKey: document.getElementById('toggleKey'),
  model: document.getElementById('model'),
  calibrate: document.getElementById('calibrate'),
  saveBtn: document.getElementById('saveBtn'),
  testBtn: document.getElementById('testBtn'),
  status: document.getElementById('status'),
  cacheSize: document.getElementById('cacheSize'),
  clearCacheBtn: document.getElementById('clearCacheBtn'),
  openCacheBtn: document.getElementById('openCacheBtn'),
  autoClear: document.getElementById('autoClear'),
  cacheStatus: document.getElementById('cacheStatus'),
};

function normalizeSettings(raw) {
  const value = raw && typeof raw === 'object' ? raw : {};
  return {
    apiKey: typeof value.apiKey === 'string' ? value.apiKey.trim() : '',
    model: (typeof value.model === 'string' && value.model.trim()) || DEFAULT_SETTINGS.model,
    calibrate: value.calibrate !== false,
  };
}

function setStatus(text, kind = '') {
  els.status.textContent = text;
  els.status.className = `status${kind ? ' ' + kind : ''}`;
}

/**
 * 扩展存储适配：正常情况下用 chrome.storage.local；
 * 若扩展尚未重新加载导致 chrome.storage 不可用，退回 localStorage（同一扩展页面共享）。
 */
function extensionStorage() {
  try {
    return globalThis.chrome?.storage?.local || null;
  } catch {
    return null;
  }
}

async function readStoredSettings() {
  const storage = extensionStorage();
  if (storage) {
    try {
      const data = await storage.get(SETTINGS_KEY);
      const stored = data?.[SETTINGS_KEY];
      if (stored) return stored;
    } catch {
      /* chrome.storage 不可用时退回 localStorage */
    }
  }
  let local = null;
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    local = raw ? JSON.parse(raw) : null;
  } catch {
    local = null;
  }
  // 旧版本/权限未生效时可能只写进了 localStorage：迁移到 chrome.storage，避免重载后丢失
  if (local && storage) {
    try {
      await storage.set({ [SETTINGS_KEY]: local });
    } catch {
      /* 迁移失败不影响本次读取 */
    }
  }
  return local;
}

async function writeStoredSettings(settings) {
  const storage = extensionStorage();
  if (storage) {
    try {
      await storage.set({ [SETTINGS_KEY]: settings });
      return;
    } catch {
      /* chrome.storage 不可用时退回 localStorage */
    }
  }
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

async function loadSettings() {
  let stored = null;
  try {
    stored = await readStoredSettings();
  } catch {
    stored = null;
  }
  const settings = normalizeSettings(stored);
  els.apiKey.value = settings.apiKey;
  const allowedModels = Array.from(els.model.options || []).map((option) => option.value);
  els.model.value = allowedModels.includes(settings.model) ? settings.model : DEFAULT_SETTINGS.model;
  els.calibrate.checked = settings.calibrate;
  return settings;
}

function readForm() {
  return normalizeSettings({
    apiKey: els.apiKey.value,
    model: els.model.value,
    calibrate: els.calibrate.checked,
  });
}

async function saveSettings() {
  const settings = readForm();
  els.saveBtn.disabled = true;
  try {
    await writeStoredSettings(settings);
    els.model.value = settings.model;
    setStatus('已保存。回到侧边栏后立即生效。', 'ok');
  } catch (err) {
    setStatus(`保存失败：${err?.message || err}`, 'error');
  } finally {
    els.saveBtn.disabled = false;
  }
}

async function testConnection() {
  const settings = readForm();
  if (!settings.apiKey) {
    setStatus('请先填写 API Key 再测试。', 'error');
    return;
  }
  els.testBtn.disabled = true;
  setStatus(`正在连接 ${settings.model}…`);
  try {
    const response = await fetch(API_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${settings.apiKey}`,
      },
      body: JSON.stringify({
        model: settings.model,
        messages: [
          { role: 'system', content: '你是中文校对助手，只修正错别字和标点，直接输出文本。' },
          { role: 'user', content: '今天天气真好我们一起去公园玩吧' },
        ],
        thinking: { type: 'disabled' },
        temperature: 0.1,
        max_tokens: 64,
      }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(data?.error?.message || `HTTP ${response.status}`);
    }
    const content = data?.choices?.[0]?.message?.content?.trim();
    setStatus(`连接成功（${settings.model}）：${content || '模型已响应'}`, 'ok');
  } catch (err) {
    setStatus(`连接失败：${err?.message || err}`, 'error');
  } finally {
    els.testBtn.disabled = false;
  }
}

function setCacheStatus(text, kind = '') {
  els.cacheStatus.textContent = text;
  els.cacheStatus.className = `status${kind ? ' ' + kind : ''}`;
}

/** 设置页通过扩展后台访问本地服务（与侧边栏使用同一套消息通道）。 */
async function callBackground(type, payload = {}, timeoutMs = 20000) {
  let timer = null;
  try {
    const response = await Promise.race([
      chrome.runtime.sendMessage({ type, ...payload }),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('扩展后台无响应')), timeoutMs);
      }),
    ]);
    if (!response) throw new Error('扩展后台没有响应');
    if (!response.ok) throw new Error(response.error || '调用失败');
    return response.data;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function refreshCache() {
  try {
    const data = await callBackground('service:cache', {}, 8000);
    const cache = data?.cache || {};
    els.cacheSize.textContent = `${cache.human || '0 B'}${cache.workspaces ? ` · ${cache.workspaces} 个目录` : ''}`;
    els.cacheSize.title = cache.root ? `目录：${cache.root}` : '';
    setCacheStatus('');
  } catch (err) {
    els.cacheSize.textContent = '—';
    setCacheStatus(`无法读取缓存：${err?.message || err}`, 'error');
  }
}

async function clearCache() {
  els.clearCacheBtn.disabled = true;
  setCacheStatus('正在清除缓存…');
  try {
    const data = await callBackground('service:cacheClear', {}, 20000);
    const cleared = data?.cleared || {};
    setCacheStatus(`已清除 ${cleared.removed || 0} 个目录，释放 ${cleared.freed_human || '0 B'}`, 'ok');
    await refreshCache();
  } catch (err) {
    setCacheStatus(`清除失败：${err?.message || err}`, 'error');
  } finally {
    els.clearCacheBtn.disabled = false;
  }
}

async function openCacheLocation() {
  els.openCacheBtn.disabled = true;
  setCacheStatus('正在打开缓存位置…');
  try {
    const data = await callBackground('service:cacheReveal', {}, 25000);
    setCacheStatus(`已打开：${data?.path || '缓存目录'}`, 'ok');
  } catch (err) {
    setCacheStatus(`打开失败：${err?.message || err}`, 'error');
  } finally {
    els.openCacheBtn.disabled = false;
  }
}

els.toggleKey.addEventListener('click', () => {
  const masked = els.apiKey.classList.toggle('masked');
  els.toggleKey.textContent = masked ? '显示' : '隐藏';
});

els.saveBtn.addEventListener('click', saveSettings);
els.testBtn.addEventListener('click', testConnection);

// 媒体缓存：开关保存在 localStorage，侧边栏在提取完成时读取同一键
els.autoClear.checked = localStorage.getItem(AUTO_CLEAR_KEY) === '1';
els.autoClear.addEventListener('change', () => {
  localStorage.setItem(AUTO_CLEAR_KEY, els.autoClear.checked ? '1' : '0');
  setCacheStatus(els.autoClear.checked ? '已开启：提取后自动清理媒体' : '已关闭自动清理', 'ok');
});
els.clearCacheBtn.addEventListener('click', clearCache);
els.openCacheBtn.addEventListener('click', openCacheLocation);

loadSettings().catch((err) => setStatus(`读取设置失败：${err?.message || err}`, 'error'));
refreshCache();

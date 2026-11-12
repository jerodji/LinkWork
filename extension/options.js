/** LinkWork 设置页：保存智谱 API Key 与 GLM 模型配置。 */

const SETTINGS_KEY = 'linkwork.settings';
const DEFAULT_SETTINGS = { apiKey: '', model: 'glm-4.7-flash', calibrate: true };
const API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions';

const els = {
  apiKey: document.getElementById('apiKey'),
  toggleKey: document.getElementById('toggleKey'),
  model: document.getElementById('model'),
  calibrate: document.getElementById('calibrate'),
  saveBtn: document.getElementById('saveBtn'),
  testBtn: document.getElementById('testBtn'),
  status: document.getElementById('status'),
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
      return data?.[SETTINGS_KEY] ?? null;
    } catch {
      /* chrome.storage 不可用时退回 localStorage */
    }
  }
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
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
  els.model.value = settings.model;
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
  setStatus('正在连接 GLM-4.7-Flash…');
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
    setStatus(`连接成功：${content || '模型已响应'}`, 'ok');
  } catch (err) {
    setStatus(`连接失败：${err?.message || err}`, 'error');
  } finally {
    els.testBtn.disabled = false;
  }
}

els.toggleKey.addEventListener('click', () => {
  const hidden = els.apiKey.type === 'password';
  els.apiKey.type = hidden ? 'text' : 'password';
  els.toggleKey.textContent = hidden ? '隐藏' : '显示';
});

els.saveBtn.addEventListener('click', saveSettings);
els.testBtn.addEventListener('click', testConnection);

loadSettings().catch((err) => setStatus(`读取设置失败：${err?.message || err}`, 'error'));

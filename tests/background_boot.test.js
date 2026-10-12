/**
 * 验证 background.js 的顶层代码能否在 MV3 service worker 环境下正常初始化。
 *
 * 重点回归：
 *   - 之前 manifest 声明了 "type": "module"，而代码使用 importScripts()，
 *     module worker 没有该 API，会抛 ReferenceError 导致整个后台脚本失效
 *     （表现：点插件图标毫无反应）。本测试会以"经典 worker"环境执行顶层代码。
 *   - chrome.sidePanel 缺失时不应崩溃。
 *   - 消息路由要能返回结构化错误，而不是静默失败。
 *
 * 运行： node tests/background_boot.test.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const MANIFEST = path.join(ROOT, 'extension', 'manifest.json');
const BACKGROUND = path.join(ROOT, 'extension', 'background.js');
const EXTRACTOR = path.join(ROOT, 'extension', 'extractor.js');

let passed = 0;
let failed = 0;

function check(name, condition, detail) {
  if (condition) {
    passed += 1;
    console.log(`  ✅ ${name}`);
  } else {
    failed += 1;
    console.log(`  ❌ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

/** 构造一个模拟的 MV3 service worker 环境 */
function makeWorkerEnv({ withSidePanel = true } = {}) {
  const listeners = { message: [], installed: [], actionClicked: [], connect: [] };
  const calls = { setPanelBehavior: [], open: [], nativeMessages: [] };
  const storage = {};

  const chrome = {
    runtime: {
      onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
      onMessage: { addListener: (fn) => listeners.message.push(fn) },
      onConnect: { addListener: (fn) => listeners.connect.push(fn) },
      getURL: (p) => `chrome-extension://fakeid/${p}`,
    },
    action: { onClicked: { addListener: (fn) => listeners.actionClicked.push(fn) } },
    tabs: { create: async () => ({ id: 1 }), remove: async () => {}, get: async () => ({ status: 'complete' }), onUpdated: { addListener() {}, removeListener() {} } },
    scripting: { executeScript: async () => [{ result: {} }] },
    cookies: { getAll: async () => [] },
    storage: { local: { get: async (key) => (key in storage ? { [key]: storage[key] } : {}), set: async (items) => { Object.assign(storage, items); } } },
    // native messaging：默认模拟「未安装本机启动器」，各用例可按需覆盖
    sendNativeMessage: async (...args) => {
      calls.nativeMessages.push(args);
      throw new Error('Specified native messaging host not found.');
    },
    sidePanel: withSidePanel
      ? {
          setPanelBehavior: (arg) => {
            calls.setPanelBehavior.push(arg);
            return Promise.resolve();
          },
          open: (arg) => {
            calls.open.push(arg);
            return Promise.resolve();
          },
        }
      : undefined,
  };

  const sandbox = {
    chrome,
    console,
    fetch: async () => ({ ok: true, status: 200, text: async () => '{}' }),
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    AbortController,
    URL,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Set,
    Map,
    Math,
    Date,
    Promise,
    Error,
    // 经典 worker 可用 importScripts；这里模拟加载 extractor.js
    importScripts: (...names) => {
      for (const name of names) {
        if (name === 'extractor.js') {
          vm.runInContext(fs.readFileSync(EXTRACTOR, 'utf8'), context);
        }
      }
    },
  };
  const context = vm.createContext(sandbox);
  // importScripts 内部需要引用 context，先声明再赋值
  sandbox.importScripts = (...names) => {
    for (const name of names) {
      if (name === 'extractor.js') {
        vm.runInContext(fs.readFileSync(EXTRACTOR, 'utf8'), context);
      }
    }
  };
  return { context, listeners, calls };
}

function boot(envOptions) {
  const env = makeWorkerEnv(envOptions);
  const src = fs.readFileSync(BACKGROUND, 'utf8');
  vm.runInContext(src, env.context);
  return env;
}

/**
 * 模拟一条侧边栏长连接（chrome.runtime.Port）。
 * port.exitOnPostMessage 用来模拟"侧边栏已被关掉"时 postMessage 抛错。
 */
function makePort(name) {
  const port = {
    name,
    sender: undefined,
    posted: [],
    exitOnPostMessage: false,
    messageListeners: [],
    disconnectListeners: [],
    onMessage: { addListener: (fn) => port.messageListeners.push(fn) },
    onDisconnect: { addListener: (fn) => port.disconnectListeners.push(fn) },
    postMessage(message) {
      if (port.exitOnPostMessage) throw new Error('Attempting to use a disconnected port object');
      port.posted.push(message);
    },
    disconnect() {
      for (const fn of [...port.disconnectListeners]) fn(port);
    },
  };
  return port;
}

/** 走一遍真实的 onConnect → 首轮健康检查，返回 { port, env } */
async function openPanelLink(env, { fetchImpl } = {}) {
  if (fetchImpl) env.context.fetch = fetchImpl;
  const port = makePort('linkwork-panel');
  // 侧边栏扩展页没有 tab，windowId 直接在 sender 上（与真实 API 一致）
  port.sender = { windowId: 7 };
  for (const fn of env.listeners.connect) fn(port);
  await new Promise((resolve) => setTimeout(resolve, 10));
  return { port, env };
}
/** 记录定时器的假环境，用于验证心跳的建立与清理 */
function installFakeTimers(env) {
  const timers = { intervals: [], cleared: [] };
  env.context.setInterval = (fn, ms) => {
    const id = { fn, ms, index: timers.intervals.length };
    timers.intervals.push(id);
    return id;
  };
  env.context.clearInterval = (id) => { timers.cleared.push(id); };
  return timers;
}

// --------------------------------------------------------------------------- //
async function main() {
console.log('background.js 启动与路由测试');

console.log('\n[1] manifest 配置正确性');
{
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
  check('声明了 sidePanel 权限', (m.permissions || []).includes('sidePanel'), JSON.stringify(m.permissions));
  check('声明了 side_panel.default_path', Boolean(m.side_panel?.default_path), JSON.stringify(m.side_panel));
  check('包含 scripting 权限（注入取数脚本需要）', (m.permissions || []).includes('scripting'));
  check('包含 cookies 权限（读取页面 cookie）', (m.permissions || []).includes('cookies'));
  check(
    'service worker 未声明 type:module（否则 importScripts 不可用）',
    m.background && m.background.type === undefined,
    JSON.stringify(m.background),
  );
  const bg = fs.readFileSync(BACKGROUND, 'utf8');
  if (bg.includes('importScripts(')) {
    check('使用 importScripts 时必须是经典 worker（二者一致）', m.background.type === undefined);
  }
  check('侧边栏页面文件存在', fs.existsSync(path.join(ROOT, 'extension', m.side_panel.default_path)));
  for (const size of ['16', '32', '48', '128']) {
    check(`图标 icons/${size}.png 存在`, fs.existsSync(path.join(ROOT, 'extension', 'icons', `${size}.png`)));
  }
}

console.log('\n[2] 顶层代码初始化（加载即崩会在此暴露）');
{
  let env;
  let err = null;
  try {
    env = boot();
  } catch (e) {
    err = e;
  }
  check('顶层代码无异常', err === null, err && `${err.name}: ${err.message}`);
  if (env) {
    check('注册了 onMessage 监听', env.listeners.message.length > 0);
    check('注册了 action.onClicked 监听', env.listeners.actionClicked.length > 0);
    check(
      '已设置 openPanelOnActionClick = true',
      env.calls.setPanelBehavior.some((a) => a && a.openPanelOnActionClick === true),
      JSON.stringify(env.calls.setPanelBehavior),
    );
    check('pageExtractor 已通过 importScripts 注入', vm.runInContext('typeof pageExtractor', env.context) === 'function');
  }
}

console.log('\n[3] sidePanel API 缺失时不应崩溃（兼容性）');
{
  let env;
  let err = null;
  try {
    env = boot({ withSidePanel: false });
  } catch (e) {
    err = e;
  }
  check('缺少 sidePanel 时仍能初始化', err === null, err && `${err.name}: ${err.message}`);
  check('仍注册了消息路由', env ? env.listeners.message.length > 0 : false);
}

console.log('\n[4] 消息路由返回结构化错误（不静默失败）');
{
  const env = boot();
  const send = (msg) =>
    new Promise((resolve) => {
      const listener = env.listeners.message[0];
      const kept = listener(msg, {}, resolve);
      if (kept !== true) resolve({ ok: false, error: 'listener 未返回 true（无法异步响应）' });
    });

  const unknown = await send({ type: 'no:such:type' });
  check('未知消息类型返回 ok:false', unknown && unknown.ok === false, JSON.stringify(unknown));
  check('错误信息可读', typeof unknown.error === 'string' && unknown.error.length > 0, unknown.error);

  const health = await send({ type: 'service:health' });
  check('service:health 能走通并返回数据', health && health.ok === true, JSON.stringify(health).slice(0, 120));
}

console.log('\n[5] 侧边栏手动打开兜底');
{
  const env = boot();
  const clickHandler = env.listeners.actionClicked[0];
  await clickHandler({ windowId: 7 });
  check('onClicked 会调用 sidePanel.open', env.calls.open.length === 1, JSON.stringify(env.calls.open));
  check('传入了正确的 windowId', env.calls.open[0] && env.calls.open[0].windowId === 7, JSON.stringify(env.calls.open[0]));
}

console.log('\n[6] 本地服务响应契约（同步接口需解开 result 外层）');
{
  // 让 fetch 依次返回三种真实响应形态
  const responses = [
    { label: 'format', body: { ok: true, result: { text: '这是整理后的文案', method: 'html' } } },
    { label: 'ocr(job)', body: { ok: true, job: { id: 'job123', state: 'pending' } } },
    { label: 'health', body: { ok: true, name: 'linkwork', asr: [{ name: 'faster-whisper', available: true }] } },
  ];
  const queue = [...responses];
  const env = makeWorkerEnv();
  env.context.fetch = async () => {
    const next = queue.shift() || { body: {} };
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify(next.body),
    };
  };
  const src = fs.readFileSync(BACKGROUND, 'utf8');
  vm.runInContext(src, env.context);
  const listener = env.listeners.message[0];
  const send = (msg) =>
    new Promise((resolve) => {
      listener(msg, {}, resolve);
    });

  const fmt = await send({ type: 'service:format', payload: { text: 'x' } });
  check('service:format 解开 result 外层（直接拿到文案对象）',
    fmt?.data?.text === '这是整理后的文案', JSON.stringify(fmt).slice(0, 120));

  const ocr = await send({ type: 'service:ocr', payload: { images: [] } });
  check('service:ocr 保留 job 外层（供轮询 job.id）',
    ocr?.data?.job?.id === 'job123', JSON.stringify(ocr).slice(0, 120));

  const health = await send({ type: 'service:health' });
  check('service:health 原样返回（含 asr 列表）',
    Array.isArray(health?.data?.asr), JSON.stringify(health).slice(0, 120));

  // 关键回归：已完成任务的响应是 {id,state,stage,progress,result:{...}}，
  // 它同样含 result 键。若被「解 result 外层」的逻辑误伤，state 会丢失，
  // 侧边栏就永远等不到完成（表现为卡在某个百分比）。
  queue.push({
    body: {
      id: 'job-done',
      kind: 'transcribe',
      state: 'done',
      stage: '完成',
      progress: 1.0,
      result: { text: '已完成的文案内容', char_count: 8 },
    },
  });
  const doneJob = await send({ type: 'service:job', jobId: 'job-done' });
  check(
    '已完成任务不被解开 result 外层（必须保留 state）',
    doneJob?.data?.state === 'done',
    JSON.stringify(doneJob).slice(0, 150),
  );
  check(
    '已完成任务仍带 result 供侧边栏直接取用',
    typeof doneJob?.data?.result?.text === 'string',
    JSON.stringify(doneJob?.data?.result).slice(0, 120),
  );

  await testSidepanelCallHasTimeout();
  await testPanelAutoConnectLifecycle();
  await testServiceAutoStart();
}

// --------------------------------------------------------------------------- //
// 打开侧边栏 = 建立服务连接；关闭侧边栏 / 关闭浏览器 = 断开
// --------------------------------------------------------------------------- //
async function testPanelAutoConnectLifecycle() {
  console.log('\n[8] 侧边栏开关驱动服务连接（打开即连、关闭即断）');

  // --- 后台侧 ---
  const env = boot();
  check('注册了 runtime.onConnect 监听', env.listeners.connect.length > 0);

  const healthBody = { ok: true, name: 'linkwork', version: '9.9.9', asr: [{ name: 'faster-whisper', available: true }] };
  const apiCalls = [];
  const healthyFetch = async (url) => {
    apiCalls.push(String(url));
    return { ok: true, status: 200, text: async () => JSON.stringify(healthBody) };
  };
  const timers = installFakeTimers(env);
  const { port } = await openPanelLink(env, { fetchImpl: healthyFetch });

  check('建立连接后登记了 1 条活动连接', env.context.__linkworkPanelBridge.panelLinks.size === 1);
  check('连接建立即做了一次健康检查', apiCalls.some((u) => u.includes('/api/health')), JSON.stringify(apiCalls));
  check(
    '把连接状态推给了侧边栏',
    port.posted.some((m) => m.type === 'service:state' && m.status === 'ok'),
    JSON.stringify(port.posted),
  );
  check(
    '推送内容带上服务版本与 ASR 名称',
    port.posted.some((m) => m.status === 'ok' && String(m.text).includes('faster-whisper') && String(m.title).includes('v9.9.9')),
    JSON.stringify(port.posted),
  );
  check('起了心跳定时器（间隔即为探活间隔）',
    timers.intervals.length === 1 && timers.intervals[0].ms === env.context.__linkworkPanelBridge.PANEL_HEARTBEAT_MS,
    JSON.stringify(timers.intervals.map((t) => t.ms)));
  check('连接对象记录了 windowId', env.context.__linkworkPanelBridge.panelLinks.get(port)?.windowId === 7);
  // 侧边栏关闭（浏览器会触发 onDisconnect）
  const link = env.context.__linkworkPanelBridge.panelLinks.get(port);
  port.disconnect();
  check('断开后连接表清空', env.context.__linkworkPanelBridge.panelLinks.size === 0);
  check('断开后清掉了心跳定时器（不再探活）', timers.cleared.includes(link.timer), JSON.stringify(timers.cleared.map(String)));

  // 服务未启动时也要给出明确状态，而不是静默
  const failingEnv = boot();
  const failing = await openPanelLink(failingEnv, {
    fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
  });
  check(
    '服务不可达时推送 fail 状态（附自动重连说明）',
    failing.port.posted.some((m) => m.status === 'fail' && String(m.title).includes('自动重连')),
    JSON.stringify(failing.port.posted),
  );
  failing.port.disconnect();

  // 侧边栏已关掉后探活推送不能抛错（否则会中断后台的查询流程）
  const goneEnv = boot();
  let gonePort;
  const gone = await openPanelLink(goneEnv, {
    fetchImpl: async () => {
      gonePort.exitOnPostMessage = true; // 推送时对端已消失
      return { ok: true, status: 200, text: async () => JSON.stringify(healthBody) };
    },
  });
  gonePort = gone.port;
  check('向已关闭的连接推送消息不会抛错', goneEnv.context.__linkworkPanelBridge.panelLinks.size === 1);
  gone.port.disconnect();

  // --- 侧边栏侧 ---
  const sidepanel = fs.readFileSync(path.join(ROOT, 'extension', 'sidepanel.js'), 'utf8');
  check(
    '侧边栏打开时主动建立连接',
    /chrome\.runtime\.connect\(\{\s*name:\s*'linkwork-panel'\s*\}\)/.test(sidepanel),
    '未找到 chrome.runtime.connect({name:...})',
  );
  check(
    '连接名与后台约定一致（linkwork-panel）',
    /const PANEL_PORT_NAME = 'linkwork-panel'/.test(fs.readFileSync(BACKGROUND, 'utf8')),
    'background.js 未定义同名常量',
  );
  check('侧边栏监听后台推送的状态', /onMessage\.addListener/.test(sidepanel));
  check('侧边栏监听断开事件', /onDisconnect\.addListener/.test(sidepanel));
  check('初始化流程里调用了 connectPanel()', /connectPanel\(\);\s*\n\s*await refreshCache/.test(sidepanel));
  check('断开后有自动重连', /scheduleReconnect/.test(sidepanel) && /setInterval/.test(sidepanel));
  check(
    '关闭侧边栏/浏览器时主动断开连接',
    /beforeunload[\s\S]{0,400}?port\?\.disconnect\(\)/.test(sidepanel),
    '未在 beforeunload 里断开连接',
  );
  check(
    '不再各查各的健康状态（状态由连接推送）',
    !/async function refreshHealth/.test(sidepanel),
    'refreshHealth 仍在，会与连接推送的状态互相覆盖',
  );

  // 界面上的「连接」开关状态，便于用户直观确认开关行为
  const panelHtml = fs.readFileSync(path.join(ROOT, 'extension', 'sidepanel.html'), 'utf8');
  check('侧边栏有连接状态标签', /id="connState"/.test(panelHtml), 'sidepanel.html 缺少 #connState');
  check('连接状态随服务状态更新', /setConnState\(/.test(sidepanel));
  check('连上时显示「连接已开启」', /连接已开启/.test(sidepanel));
  check('连不上时显示「服务未连接」', /服务未连接/.test(sidepanel));
  check(
    '提取中不被连接状态覆盖阶段提示',
    /if \(!busy\) \{[\s\S]{0,200}?setStatus\('ok'/.test(sidepanel),
    'applyServiceState 未在 busy 时让步于阶段提示',
  );
}


}

// --------------------------------------------------------------------------- //
// 侧边栏消息调用必须有超时保护
// --------------------------------------------------------------------------- //
async function testSidepanelCallHasTimeout() {
  console.log('\n[7] 侧边栏消息调用必须带超时保护');
  const sidepanel = fs.readFileSync(path.join(ROOT, 'extension', 'sidepanel.js'), 'utf8');

  // call() 必须用 Promise.race 加超时；否则 service worker 被回收时
  // sendMessage 永不 resolve，界面会永久停在最后一条进度（真实踩过）
  check(
    'call() 使用 Promise.race 实现超时',
    /async function call\([\s\S]{0,1200}?Promise\.race\(/.test(sidepanel),
    '未找到 Promise.race',
  );
  check(
    'call() 支持超时参数',
    /async function call\(type, payload = \{\}, timeoutMs = \d+\)/.test(sidepanel),
    'call 缺少 timeoutMs 参数',
  );
  check('超时会抛出可读错误', /扩展后台无响应/.test(sidepanel), '未找到超时提示文案');
  check(
    'waitJob 对轮询失败做重试',
    /轮询失败（第 \$\{consecutiveErrors\} 次）/.test(sidepanel),
    'waitJob 未做轮询重试',
  );
  check(
    'waitJob 完成时记录轮询次数',
    /轮询 \$\{polls\} 次/.test(sidepanel),
    'waitJob 未记录轮询次数',
  );
  // 后端在 state=done 时已带回 result；必须「拿到就用」，
  // 否则错过一次 done + 服务重启（任务表在内存）就永远拿不回结果
  check(
    'waitJob 拿到 result 立即返回',
    /if \(job\.result\) \{[\s\S]{0,120}?return job\.result;/.test(sidepanel),
    'waitJob 未优先使用 job.result',
  );
  check(
    '响应缺 state 时打印原始内容',
    /响应结构异常（第 \$\{consecutiveErrors\} 次，无 state 字段）/.test(sidepanel),
    '未打印异常响应内容',
  );
  check(
    '异常响应可重试而非直接崩',
    /连续 \$\{consecutiveErrors\} 次拿到异常响应/.test(sidepanel)
      && /await sleep\(1500\);\s*\n\s*continue;/.test(sidepanel),
    '异常响应未做重试',
  );
  check(
    'job_not_found 有针对性提示',
    /job_not_found\|任务不存在/.test(sidepanel),
    '未针对任务丢失给出提示',
  );
}

// --------------------------------------------------------------------------- //
// 服务没跑时自动拉起（native host）
// --------------------------------------------------------------------------- //
async function testServiceAutoStart() {
  console.log('\n[9] 通过 native host 自动启动本地服务');

  const manifest = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
  check(
    'manifest 声明了 nativeMessaging 权限',
    (manifest.permissions || []).includes('nativeMessaging'),
    JSON.stringify(manifest.permissions),
  );

  const hostScript = path.join(ROOT, 'native_host', 'linkwork_host.py');
  const wrapper = path.join(ROOT, 'native_host', 'linkwork-host-wrapper.sh');
  const installer = path.join(ROOT, 'scripts', 'install_native_host.py');
  const launcherSrc = path.join(ROOT, 'native_host', 'linkwork-launcher.c');
  check('native host 实现存在', fs.existsSync(hostScript));
  check('native host 包装脚本存在', fs.existsSync(wrapper));
  check('native host 注册脚本存在', fs.existsSync(installer));
  check(
    '提供 C 版启动器（macOS 上浏览器无法执行 shell 脚本，必须用二进制）',
    fs.existsSync(launcherSrc),
    '缺少 native_host/linkwork-launcher.c',
  );
  check(
    '注册脚本会编译并使用该二进制',
    /def ensure_launcher/.test(fs.readFileSync(installer, 'utf8'))
      && /program_for_manifest/.test(fs.readFileSync(installer, 'utf8')),
    '注册脚本仍把 shell 脚本写进 manifest',
  );
  check(
    '包装脚本可执行',
    fs.existsSync(wrapper) && (fs.statSync(wrapper).mode & 0o111) !== 0,
    '缺少可执行位（浏览器无法启动它）',
  );

  const hostSource = fs.readFileSync(hostScript, 'utf8');
  const installerSource = fs.readFileSync(installer, 'utf8');
  const background = fs.readFileSync(BACKGROUND, 'utf8');

  // host 名必须三处一致，否则浏览器找不到启动器
  const hostNameInBg = /const NATIVE_HOST_NAME = '([^']+)'/.exec(background)?.[1];
  const hostNameInInstaller = /HOST_NAME = "([^"]+)"/.exec(installerSource)?.[1];
  check('扩展与注册脚本使用同一个 host 名', hostNameInBg && hostNameInBg === hostNameInInstaller,
    `扩展=${hostNameInBg} 注册脚本=${hostNameInInstaller}`);

  check('host 只接受白名单动作', /action == "ping"/.test(hostSource) && /action == "start"/.test(hostSource));
  check(
    'host 不执行任意命令（启动命令固定）',
    /"-m", "linkwork", "serve", "--port"/.test(hostSource),
    '启动命令不是固定的 linkwork serve',
  );
  check(
    'host 用 4 字节长度前缀协议',
    /struct\.unpack\("<I"/.test(hostSource) && /struct\.pack\("<I"/.test(hostSource),
  );
  check(
    '注册脚本写入 allowed_origins',
    /allowed_origins/.test(installerSource) && /chrome-extension:\/\//.test(installerSource),
  );
  check(
    '注册脚本能自动识别扩展 ID',
    /def loaded_extension_ids/.test(installerSource) && /def extension_id_from_path/.test(installerSource),
  );

  // 服务不可达时必须真的去叫 native host，并把「没装启动器」说清楚
  const env = boot();
  const nativeCalls = [];
  env.context.chrome.runtime.sendNativeMessage = async (name, message) => {
    nativeCalls.push([name, message]);
    throw new Error('Specified native messaging host not found.');
  };
  const { port } = await openPanelLink(env, {
    fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
  });
  check(
    '服务不可达时调用了 native host',
    nativeCalls.length === 1 && nativeCalls[0][1]?.action === 'start',
    JSON.stringify(nativeCalls),
  );
  check(
    'native host 名与注册的一致',
    nativeCalls[0]?.[0] === env.context.__linkworkPanelBridge.NATIVE_HOST_NAME,
    nativeCalls[0]?.[0],
  );
  check(
    '未安装启动器时给出安装提示',
    port.posted.some((m) => m.status === 'fail' && /install_native_host|本机启动器|本机程序/.test(String(m.title))),
    JSON.stringify(port.posted),
  );
  check(
    '启动失败后进入冷却（不反复拉起）',
    env.context.__linkworkPanelBridge.panelLinks.get(port)?.startBlockedUntil > Date.now(),
  );
  check(
    '「没装启动器」不重试（重试也没用）',
    nativeCalls.length === 1,
    `调用了 ${nativeCalls.length} 次`,
  );

  // 「Native host has exited」是瞬时故障：应重试一次，重试成功即视为成功
  const envRetry = boot();
  envRetry.context.__linkworkPanelBridge.setNativeHostRetryDelay(0); // 测试里不等退避
  let attempts = 0;
  envRetry.context.chrome.runtime.sendNativeMessage = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('Native host has exited.');
    return { ok: true, started: true, pid: 777 };
  };
  let healthProbe = 0;
  const retried = await openPanelLink(envRetry, {
    fetchImpl: async () => {
      healthProbe += 1;
      if (healthProbe === 1) throw new TypeError('Failed to fetch');
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, name: 'linkwork', version: '1.0.0', asr: [] }) };
    },
  });
  // openPanelLink 只等 10ms，而重试链路更长，这里多给一点时间
  for (let i = 0; i < 40 && !retried.port.posted.some((m) => m.status === 'ok'); i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  check('「Native host has exited」会重试一次', attempts === 2, `实际尝试 ${attempts} 次`);
  check(
    '重试成功后界面变成已连接',
    retried.port.posted.some((m) => m.status === 'ok'),
    JSON.stringify(retried.port.posted),
  );
  retried.port.disconnect();

  // 启动成功时：先探活失败 → 叫 host → 再探活成功，最终状态为 ok
  const env2 = boot();
  let healthCalls = 0;
  env2.context.chrome.runtime.sendNativeMessage = async () => ({ ok: true, started: true, pid: 4242 });
  const second = await openPanelLink(env2, {
    fetchImpl: async (url) => {
      healthCalls += 1;
      // 第一次探测（启动前）失败，之后的探测成功
      if (healthCalls === 1) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ ok: true, name: 'linkwork', version: '9.9.9', asr: [] }),
      };
    },
  });
  check(
    '自动启动过程中推了「正在启动」状态',
    second.port.posted.some((m) => m.status === 'checking' && /启动/.test(String(m.text))),
    JSON.stringify(second.port.posted),
  );
  check(
    '启动后重新探活并推 ok',
    second.port.posted.some((m) => m.status === 'ok'),
    JSON.stringify(second.port.posted),
  );
  check('启动后确实又查了一次健康', healthCalls >= 2, `healthCalls=${healthCalls}`);
  second.port.disconnect();

  // 关闭自动启动开关后，不再尝试拉起
  const env3 = boot();
  const nativeCalls3 = [];
  env3.context.chrome.runtime.sendNativeMessage = async (...args) => {
    nativeCalls3.push(args);
    return { ok: true, started: true };
  };
  await env3.context.__linkworkPanelBridge.setAutoStart(false);
  const third = await openPanelLink(env3, {
    fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
  });
  check('关掉开关后不再自动拉起服务', nativeCalls3.length === 0, JSON.stringify(nativeCalls3));
  third.port.disconnect();
  await env3.context.__linkworkPanelBridge.setAutoStart(true);

  // 面板侧：按钮 + 开关
  const sidepanel = fs.readFileSync(path.join(ROOT, 'extension', 'sidepanel.js'), 'utf8');
  const panelHtml = fs.readFileSync(path.join(ROOT, 'extension', 'sidepanel.html'), 'utf8');
  check('面板有「启动服务」按钮', /id="startServiceBtn"/.test(panelHtml));
  check('面板有「打开时自动启动」开关', /id="autoStartService"/.test(panelHtml));
  check('按钮调用 service:start', /call\('service:start'/.test(sidepanel));
  check('开关同步给后台', /call\('service:autoStart'/.test(sidepanel));
  check('启动失败时提示安装本机启动器', /install_native_host\.py/.test(sidepanel));
}

main()
  .then(() => {
    console.log(`\n结果：${passed} 通过 / ${failed} 失败`);
    process.exit(failed === 0 ? 0 : 1);
  })
  .catch((err) => {
    console.error('测试执行异常：', err);
    process.exit(1);
  });

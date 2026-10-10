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
  const listeners = { message: [], installed: [], actionClicked: [] };
  const calls = { setPanelBehavior: [], open: [] };

  const chrome = {
    runtime: {
      onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
      onMessage: { addListener: (fn) => listeners.message.push(fn) },
      getURL: (p) => `chrome-extension://fakeid/${p}`,
    },
    action: { onClicked: { addListener: (fn) => listeners.actionClicked.push(fn) } },
    tabs: { create: async () => ({ id: 1 }), remove: async () => {}, get: async () => ({ status: 'complete' }), onUpdated: { addListener() {}, removeListener() {} } },
    scripting: { executeScript: async () => [{ result: {} }] },
    cookies: { getAll: async () => [] },
    storage: { local: { get: async () => ({}), set: async () => {} } },
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

main()
  .then(() => {
    console.log(`\n结果：${passed} 通过 / ${failed} 失败`);
    process.exit(failed === 0 ? 0 : 1);
  })
  .catch((err) => {
    console.error('测试执行异常：', err);
    process.exit(1);
  });

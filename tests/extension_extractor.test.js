/**
 * 插件取数逻辑测试（Node 环境，无需浏览器）。
 *
 * 直接从 extension/background.js 里取出 pageExtractor，用最小 DOM shim 喂真实/构造的页面，
 * 验证各平台分支能正确产出字段。
 *
 * 运行：
 *   node tests/extension_extractor.test.js
 *
 * 夹具说明：
 *   - tests/fixtures/xiaohongshu.html   构造的小红书笔记页（含 __INITIAL_STATE__）
 *   - tests/fixtures/wechat.html        构造的公众号文章页
 *   - tests/fixtures/bilibili.html      真实 B 站页面（可选，缺失则跳过该用例）
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const BG = path.join(ROOT, 'extension', 'background.js');
const FIXTURES = path.join(__dirname, 'fixtures');

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

/** 加载独立的取数模块（extension/extractor.js） */
function loadExtractor() {
  const modulePath = path.join(ROOT, 'extension', 'extractor.js');
  delete require.cache[require.resolve(modulePath)];
  const mod = require(modulePath);
  if (typeof mod.pageExtractor !== 'function') {
    throw new Error('extractor.js 没有导出 pageExtractor');
  }
  return mod.pageExtractor;
}

/** 构造最小 DOM shim，让 pageExtractor 可以在 Node 里跑 */
function runExtractor(html, href, options = {}) {
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => ({
    textContent: m[1],
  }));
  const metas = {};
  for (const m of html.matchAll(/<meta[^>]+(?:property|name)="([^"]+)"[^>]+content="([^"]*)"/g)) {
    if (!metas[m[1]]) metas[m[1]] = m[2];
  }
  const titleMatch = html.match(/<title[^>]*>([^<]*)<\/title>/);
  const videoEl = options.video || null;
  if (options.debug) sandboxDebug = true;
  const cleanText = html
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  const document = {
    scripts,
    getElementById(id) {
      // 取 <script id="X" ...>内容</script>（如抖音的 RENDER_DATA）
      const m = html.match(new RegExp(`<script[^>]+id="${id}"[^>]*>([\\s\\S]*?)</script>`, 'i'));
      return m ? { textContent: m[1] } : null;
    },
    title: titleMatch ? titleMatch[1] : '',
    body: { innerText: cleanText.slice(0, 8000), textContent: cleanText.slice(0, 8000) },
    querySelector(sel) {
      const metaMatch = sel.match(/meta\[(?:property|name)="([^"]+)"\]/);
      if (metaMatch) {
        return metas[metaMatch[1]] ? { getAttribute: () => metas[metaMatch[1]] } : null;
      }
      if (sel.includes(',')) {
        for (const part of sel.split(',')) {
          const found = this.querySelector(part.trim());
          if (found) return found;
        }
        return null;
      }
      if (sel === 'video') return videoEl;
      // 属性选择器：[data-e2e="video-desc"] 等
      const attrMatch = sel.match(/^\[([\w-]+)="?([^"\]]+)"?\]$/);
      if (attrMatch) {
        const [, attr, value] = attrMatch;
        const re = new RegExp(`${attr}="${value}"[^>]*>([\\s\\S]*?)<`, 'i');
        const m = html.match(re);
        if (m) {
          const text = m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
          return { innerText: text, textContent: text };
        }
        return null;
      }
      if (sel === 'article' || sel === 'main') return null;
      if (sel.includes('js_content')) {
        if (!html.includes('id="js_content"')) return null;
        const inner = html.split('id="js_content"')[1] || '';
        const text = inner.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
        return { innerText: text, textContent: text };
      }
      return null;
    },
    querySelectorAll() {
      return [];
    },
  };

  const sandbox = {
    document,
    location: { href, hostname: new URL(href).hostname },
    console,
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
    // 页面里本就有的全局函数，取数逻辑会用到（曾因缺失导致误报）
    decodeURIComponent,
    decodeURI,
    encodeURIComponent,
  };
  // extractor.js 内部只依赖 document / location，这里用 vm 注入 shim 后执行
  const context = vm.createContext(sandbox);
  const extractorSource = fs.readFileSync(path.join(ROOT, 'extension', 'extractor.js'), 'utf8');
  vm.runInContext(extractorSource, context);
  return vm.runInContext('pageExtractor()', context);
}

let sandboxDebug = false;
function readFixture(name) {
  const file = path.join(FIXTURES, name);
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
}

// --------------------------------------------------------------------------- //
console.log('插件取数逻辑测试');

console.log('\n[1] 小红书图文笔记');
{
  const html = readFixture('xiaohongshu.html');
  const out = runExtractor(html, 'https://www.xiaohongshu.com/explore/abc');
  check('识别平台为小红书', out.platform === '小红书', out.platform);
  check('提取标题', out.title === '三分钟学会文案提取', out.title);
  check('提取作者', out.author === '效率小张', out.author);
  check('提取正文', (out.desc || '').includes('链接文案提取方法'), out.desc);
  check('提取 2 张图片', out.images.length === 2, JSON.stringify(out.images));
  check('判定为图文类型', out.kind === 'image_text', out.kind);
}

console.log('\n[2] 微信公众号文章');
{
  const html = readFixture('wechat.html');
  const out = runExtractor(html, 'https://mp.weixin.qq.com/s/abc');
  check('识别平台为微信公众号', out.platform === '微信公众号', out.platform);
  check('提取正文', (out.text || '').includes('公众号正文第一段'), (out.text || '').slice(0, 60));
  check('提取作者', out.author === '作者小明', out.author);
}

console.log('\n[3] B 站视频页（真实页面夹具）');
{
  const html = readFixture('bilibili.html');
  if (!html) {
    console.log('  ⏭  跳过：缺少 tests/fixtures/bilibili.html');
  } else {
    const out = runExtractor(html, 'https://www.bilibili.com/video/BV1GJ411x7h7');
    check('识别平台为B站', out.platform === 'B站', out.platform);
    check('提取 BV 号', out.bvid === 'BV1GJ411x7h7', out.bvid);
    check('标题取自 videoData（非推荐卡片）', (out.title || '').includes('Never Gonna Give You Up'), out.title);
    check('作者取自 videoData.owner', out.author === '索尼音乐中国', out.author);
    check('时长解析正确', out.mediaDurationSec === 213, String(out.mediaDurationSec));
    check('无简介时不报错（后续走 resolve）', !out.error, out.error);
  }
}

console.log('\n[4] 抖音页面（DOM 文案优先）');
{
  const html = `<!doctype html><html><head><title>抖音</title></head><body>
    <div data-e2e="video-desc">这是抖音视频的文案内容，用来验证 DOM 提取路径是否生效。</div>
    <div data-e2e="video-author-name">测试作者</div>
    <video src="https://v3-web.douyinvod.com/abc/video.mp4"></video>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/1234567890', {
    video: { currentSrc: 'https://v3-web.douyinvod.com/abc/video.mp4', src: '', duration: 30 },
  });
  check('识别平台为抖音', out.platform === '抖音', out.platform);
  check('从 DOM 提取文案', (out.desc || '').includes('抖音视频的文案内容'), out.desc);
  check('从 DOM 提取作者', out.author === '测试作者', out.author);
  check('拿到媒体地址', (out.mediaUrl || '').includes('douyinvod'), out.mediaUrl);
  check('判定有媒体', out.hasMedia === true, String(out.hasMedia));
}

console.log('\n[5] 抖音脚手架页（无数据时应明确报错）');
{
  const html = `<!doctype html><html><head><title>抖音</title>
    <meta name="description" content="来抖音，记录美好生活"></head><body><div id="root"></div></body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/1234567890');
  check('给出可操作报错', Boolean(out.error), JSON.stringify(out).slice(0, 80));
  check('报错提示已登录/详情页', (out.error || '').includes('已登录'), out.error);
}

console.log('\n[6] 媒体候选过滤（排除封面图）');
{
  const html = `<!doctype html><html><head><title>t</title></head><body>
    <div data-e2e="video-desc">文案内容在这里</div>
    <script>window._ROUTER_DATA = {"loaderData":{"videoInfo":{"desc":"文案内容在这里","video":{"duration":45000,"play_addr":{"url_list":["https://v3-web.douyinvod.com/a/video.mp4","https://p3-pc.douyinpic.com/cover.jpeg"]},"cover":{"url_list":["https://p3-pc.douyinpic.com/cover.jpeg"]}}}}};</script>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/1234567890');
  const all = [out.mediaUrl, ...(out.mediaCandidates || [])].filter(Boolean);
  check('保留视频流地址', all.some((u) => u.includes('douyinvod')), JSON.stringify(all));
  check('过滤掉封面图', !all.some((u) => u.includes('douyinpic')), JSON.stringify(all));
  check('解析出时长', out.mediaDurationSec === 45, String(out.mediaDurationSec));
}

console.log('\n[7] 抖音优先取音频流（music.play_url）');
{
  const html = `<!doctype html><html><head><title>t</title></head><body>
    <div data-e2e="video-desc">这是带音乐的抖音视频文案</div>
    <script>window._ROUTER_DATA = {"loaderData":{"videoInfo":{"desc":"这是带音乐的抖音视频文案","music":{"play_url":{"url_list":["https://sf3-cdn-tos.douyinstatic.com/obj/music/audio.m4a"]}},"video":{"duration":30000,"play_addr":{"url_list":["https://v3-web.douyinvod.com/a/video.mp4"]},"cover":{"url_list":["https://p3-pc.douyinpic.com/cover.jpeg"]}}}}};</script>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/1234567890');
  check('提取到音频地址', (out.audioUrl || '').includes('music'), out.audioUrl);
  check('音频候选已归一化', Array.isArray(out.audioCandidates) && out.audioCandidates.length === 1, JSON.stringify(out.audioCandidates));
  check('仍保留视频流作为备选', (out.mediaCandidates || []).some((u) => u.includes('douyinvod')), JSON.stringify(out.mediaCandidates));
  check('判定有媒体', out.hasMedia === true, String(out.hasMedia));
}

console.log('\n[8] 没有音频流时退回视频流');
{
  const html = `<!doctype html><html><head><title>t</title></head><body>
    <div data-e2e="video-desc">无音乐的视频文案</div>
    <script>window._ROUTER_DATA = {"loaderData":{"videoInfo":{"desc":"无音乐的视频文案","video":{"duration":20000,"play_addr":{"url_list":["https://v3-web.douyinvod.com/b/video.mp4"]}}}}};</script>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/999');
  check('audioUrl 为空', out.audioUrl === '', JSON.stringify(out.audioUrl));
  check('视频流仍可用', (out.mediaUrl || out.mediaCandidates?.[0] || '').includes('douyinvod'), JSON.stringify(out.mediaCandidates));
}

console.log('\n[9] 抖音精选/推荐页（RENDER_DATA，真实数据夹具）');
{
  const html = readFixture('douyin_jingxuan.html');
  if (!html) {
    console.log('  ⏭  跳过：缺少 tests/fixtures/douyin_jingxuan.html');
  } else {
    const out = runExtractor(html, 'https://www.douyin.com/jingxuan?modal_id=7678241057914762235');
    check('识别平台为抖音', out.platform === '抖音', out.platform);
    check('从 RENDER_DATA 提取到文案', (out.desc || '').length > 10, (out.desc || '').slice(0, 40));
    check('提取到标题', Boolean(out.title), out.title);
    check('提取到作者', out.author === '漫步行者', out.author);
    check('拿到媒体候选', (out.mediaCandidates || []).length > 0, String((out.mediaCandidates || []).length));
    check('媒体候选含视频直链', (out.mediaCandidates || []).some((u) => u.includes('douyinvod')), JSON.stringify((out.mediaCandidates||[]).slice(0,1)));
    check('判定有媒体', out.hasMedia === true, String(out.hasMedia));
    check('时长解析（ms→s）', out.mediaDurationSec === 60, String(out.mediaDurationSec));
    check('未因缺少 DOM 选择器而报错', !out.error, out.error);
  }
}

console.log('\n[10] 抖音分享页 videoInfoRes 结构（真实短链解析结果）');
{
  const html = readFixture('douyin_share.html');
  if (!html) {
    console.log('  ⏭  跳过：缺少 tests/fixtures/douyin_share.html');
  } else {
    const out = runExtractor(html, 'https://www.iesdouyin.com/share/video/7682068137395498650/');
    check('识别平台为抖音', out.platform === '抖音', out.platform);
    check('从 videoInfoRes.item_list 取到文案', (out.desc || '').includes('善良和真诚'), (out.desc || '').slice(0, 40));
    check('提取作者', out.author === 'Lens9', out.author);
    check('拿到媒体地址', (out.mediaCandidates || []).length > 0, String((out.mediaCandidates || []).length));
    check('媒体地址为 playwm 直链', (out.mediaCandidates || [])[0]?.includes('playwm'), (out.mediaCandidates || [])[0]?.slice(0, 60));
    check('时长解析 22434ms→22.434s', Math.abs(out.mediaDurationSec - 22.434) < 0.01, String(out.mediaDurationSec));
    check('无 data-e2e 也不报错（纯 JSON 路径）', !out.error, out.error);
  }
}

console.log('\n[11] 通用媒体扫描（模拟抖音改键名后仍能取到媒体）');
{
  // 故意把键名全改掉，不走 _ROUTER_DATA 的固定键路径
  const html = `<!doctype html><html><head><title>t</title></head><body>
    <script>window._ROUTER_DATA = {"loaderData":{"some/other/page":{"momentTitle":"改版后的标题",
      "authorInfo":{"screenName":"改版作者"},
      "unknownStreams":{"list":[{"source":"https://v26-web.douyinvod.com/abc/video/tos/cn/tos-cn-ve-15c000-ce/xyz/?a=1"}]},
      "thumb":{"url":"https://p3-pc.douyinpic.com/cover.jpeg"},
      "durationMs":15000}}};</script>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/video/1234567890');
  check('通用扫描被触发', out.sources?.genericScan === 1, JSON.stringify(out.sources));
  check('扫到了视频直链', (out.mediaCandidates || []).some((u) => u.includes('douyinvod')), JSON.stringify(out.mediaCandidates));
  check('未把封面图当媒体', !(out.mediaCandidates || []).some((u) => u.includes('douyinpic')), JSON.stringify(out.mediaCandidates));
  check('判定有媒体', out.hasMedia === true, String(out.hasMedia));
  check('媒体来源标注为 JSON 字段', out.mediaVia === 'JSON 媒体字段', String(out.mediaVia));
}

console.log('\n[12] 诊断信息（供日志排查改版）');
{
  const html = readFixture('douyin_share.html');
  const out = runExtractor(html, 'https://www.iesdouyin.com/share/video/7682068137395498650/');
  check('返回 sources 对象', Boolean(out.sources), JSON.stringify(out.sources));
  check('命中 _ROUTER_DATA', out.sources?.routerData === 2, String(out.sources?.routerData));
  check('返回 diagnostics', Boolean(out.diagnostics?.blocks?.length), JSON.stringify(out.diagnostics));
  check('诊断列出数据块名', (out.diagnostics?.blocks || []).some((b) => b.includes('ROUTER') || b.includes('videoInfo')), JSON.stringify(out.diagnostics?.blocks));
}

console.log('\n[13] 推荐流页面的噪声不应被当成媒体（真实结构复刻）');
{
  // 复刻 douyin.com/?recommend=1 的实际情况：RENDER_DATA 里没有媒体地址，
  // 但有「抖音客户端下载包」和「安全公约链接」这两类曾被误收的噪声
  const noise = {
    app: {
      tccConfig: {
        download_info: {
          video: 'https://lf3-static.bytednsdoc.com/obj/eden-cn/ild_jw_upfbvk_lm/ljhwZthlaukjlkulzlp/download/douyin_pc_client.mp4',
        },
      },
      fan_index_info: {
        good_clip_link_list: [
          { alink: 'https://aweme.snssdk.com/falcon/fe_douyin_security_react/self_discipline_convention?id=1' },
        ],
      },
      search_scc_config: { scc_cs_white_list: ['is.snssdk.com'] },
      covers: [
        { url: 'https://p3-pc.douyinpic.com/img/cover1.jpeg' },
        { url: 'https://p3-pc.douyinpic.com/img/cover2.webp' },
      ],
    },
  };
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>抖音推荐</title></head><body>
    <div data-e2e="feed-video"><video></video></div>
    <script id="RENDER_DATA" type="application/json">${JSON.stringify(JSON.stringify(noise))}</script>
  </body></html>`;
  const out = runExtractor(html, 'https://www.douyin.com/?recommend=1');
  const all = [out.mediaUrl, ...(out.mediaCandidates || []), ...(out.audioCandidates || [])].filter(Boolean);
  check('未把客户端下载包当媒体', !all.some((u) => u.includes('douyin_pc_client')), JSON.stringify(all));
  check('未把安全公约链接当媒体', !all.some((u) => u.includes('self_discipline')), JSON.stringify(all));
  check('未把裸域名当媒体', !all.some((u) => u === 'is.snssdk.com' || u.endsWith('//is.snssdk.com')), JSON.stringify(all));
  check('未把封面图当媒体', !all.some((u) => u.includes('douyinpic')), JSON.stringify(all));
  check('媒体候选为空', all.length === 0, JSON.stringify(all));
  check('判定为无媒体', out.hasMedia === false, String(out.hasMedia));
  check('未触发通用扫描（没有真媒体）', out.sources?.genericScan === 0, JSON.stringify(out.sources));
  check('给出可操作报错', Boolean(out.error), out.error);
}

console.log('\n[14] 协议相对 / 转义媒体地址应被规范化（真实抖音形态）');
{
  const html = readFixture('douyin_protocol_relative.html');
  if (!html) {
    console.log('  ⏭  跳过：缺少 tests/fixtures/douyin_protocol_relative.html');
  } else {
    const out = runExtractor(html, 'https://www.douyin.com/video/1');
    const all = [out.mediaUrl, ...(out.mediaCandidates || [])].filter(Boolean);
    check('拿到媒体候选', all.length > 0, JSON.stringify(all));
    check('全部补全为 https://', all.every((u) => u.startsWith('https://')), JSON.stringify(all));
    check('保留 playwm / video/tos 路径', all.some((u) => u.includes('playwm') || u.includes('/video/tos/')), JSON.stringify(all));
    check('不含反斜杠残留', !all.some((u) => u.includes('\\')), JSON.stringify(all));
    check('不含 %5C 等转义残留', !all.some((u) => /%5C/i.test(u)), JSON.stringify(all));
  }
}

console.log('\n[15] 推荐流页面：双重编码 RENDER_DATA 仍能取到文案');
{
  const html = readFixture('douyin_recommend.html');
  if (!html) {
    console.log('  ⏭  跳过：缺少 tests/fixtures/douyin_recommend.html');
  } else {
    const out = runExtractor(html, 'https://www.douyin.com/?recommend=1');
    check('识别平台为抖音', out.platform === '抖音', out.platform);
    check('解析出双层编码的 RENDER_DATA', out.sources?.renderData === 2, String(out.sources?.renderData));
    check('取到文案 desc', (out.desc || '').includes('善良') || (out.desc || '').length > 10, (out.desc || '').slice(0, 40));
    check('取到作者', out.author === '漫步行者', out.author);
    check('未误判有媒体（推荐流本身无媒体地址）', out.hasMedia === false, String(out.hasMedia));
    check('媒体候选为空（噪声已过滤）', (out.mediaCandidates || []).length === 0, JSON.stringify(out.mediaCandidates));
    check('无错误（有文案可展示）', !out.error, out.error);
  }
}

console.log('\n[16] 取数函数的自包含性（序列化执行时不能引用外部变量）');
{
  const html = readFixture('douyin_recommend.html');
  const out = runExtractor(html || '<html><body></body></html>', 'https://www.douyin.com/?recommend=1');
  check('返回 build 版本号', typeof out.build === 'number' && out.build > 0, String(out.build));
  // 函数被 chrome.scripting.executeScript 序列化后单独执行，
  // 一旦引用了模块级变量就会抛 ReferenceError（曾因 EXTRACTOR_BUILD 踩过）
  check('无 ReferenceError（错误信息不含 not defined）', !/is not defined/.test(out.error || ''), out.error);

  // 静态检查：函数体内不得出现「引用但未声明且形如 CONSTANT」的标识符
  const src = fs.readFileSync(path.join(ROOT, 'extension', 'extractor.js'), 'utf8');
  const start = src.indexOf('function pageExtractor()');
  let depth = 0;
  let started = false;
  let end = -1;
  for (let i = start; i < src.length; i += 1) {
    if (src[i] === '{') { depth += 1; started = true; }
    else if (src[i] === '}') { depth -= 1; if (started && depth === 0) { end = i + 1; break; } }
  }
  const body = src.slice(start, end);
  const declared = new Set(
    [...body.matchAll(/(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  );
  const stringsAndComments = body
    .replace(/\/\/[^\n]*/g, ' ')
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '``');
  // 页面/JS 环境自带的全局对象不算「外部变量」
  const KNOWN_GLOBALS = new Set([
    'JSON', 'URL', 'DOM', 'HTML', 'CSS', 'API', 'UA', 'ID', 'OK',
    'SSR', 'CDN', 'BV', 'PC', 'IOS', 'GPU',
  ]);
  const suspicious = new Set(
    [...stringsAndComments.matchAll(/\b([A-Z][A-Z0-9_]{2,})\b/g)]
      .map((m) => m[1])
      .filter((n) => !declared.has(n) && !KNOWN_GLOBALS.has(n)),
  );
  check('函数体内无未声明的全大写常量引用', suspicious.size === 0, [...suspicious].join(', '));
}

console.log(`\n结果：${passed} 通过 / ${failed} 失败`);
process.exit(failed === 0 ? 0 : 1);

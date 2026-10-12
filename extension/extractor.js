/**
 * 页面取数逻辑（在页面"主世界"里执行）。
 *
 * 单独成文件的原因：
 *  1. 可被 tests/extension_extractor.test.js 直接加载做回归测试；
 *  2. background.js 通过 importScripts 引入（service worker 环境）。
 *
 * ⚠️ 本函数会被序列化后注入页面执行，因此：
 *   - 不能引用外部变量或闭包；
 *   - 只能使用 document / location 等页面全局对象。
 */

function pageExtractor() {
  //: 取数逻辑版本标记（排查「改了没生效」时看日志里这个数字）
  const EXTRACTOR_BUILD = 32;
  const MAX_TEXT = 200000;

  function textOf(el) {
    return (el?.innerText || el?.textContent || '').replace(/\u00a0/g, ' ').trim();
  }

  function firstNonEmpty(list) {
    for (const item of list) {
      const value = typeof item === 'string' ? item.trim() : '';
      if (value) return value;
    }
    return '';
  }

  function metaContent(names) {
    for (const name of names) {
      const el =
        document.querySelector(`meta[property="${name}"]`) ||
        document.querySelector(`meta[name="${name}"]`);
      const content = el?.getAttribute('content');
      if (content && content.trim()) return content.trim();
    }
    return '';
  }

  function isBoilerplate(text) {
    if (!text) return true;
    const patterns = [
      '来抖音，记录美好生活',
      '记录美好生活',
      '抖音短视频',
      '抖音官网',
      '发现更多精彩视频',
      '你访问的页面不见了',
      '页面不存在',
    ];
    return patterns.some((p) => text.includes(p));
  }

  // 在页面脚本文本里按大括号配平截取 JSON（用于取站点注入的 SSR 数据）
  function parseAssignedJson(marker) {
    const scripts = Array.from(document.scripts);
    for (const script of scripts) {
      const source = script.textContent || '';
      const index = source.indexOf(marker);
      if (index < 0) continue;
      const start = source.indexOf('{', index);
      if (start < 0) continue;
      let depth = 0;
      let inString = false;
      let escaped = false;
      for (let i = start; i < source.length; i += 1) {
        const ch = source[i];
        if (inString) {
          if (escaped) escaped = false;
          else if (ch === '\\') escaped = true;
          else if (ch === '"') inString = false;
          continue;
        }
        if (ch === '"') inString = true;
        else if (ch === '{') depth += 1;
        else if (ch === '}') {
          depth -= 1;
          if (depth === 0) {
            try {
              return JSON.parse(source.slice(start, i + 1));
            } catch {
              return null;
            }
          }
        }
      }
    }
    return null;
  }

  /**
   * 深度查找。
   * @param objectOnly 为 true 时只返回对象（用于 videoInfo / aweme_detail 这类容器），
   *                   否则只返回非空字符串（用于 desc / nickname 这类叶子字段）。
   * 注意：必须区分两者——否则找容器时会在其内部命中第一个字符串字段而提前返回。
   */
  /** 解析 <script id="RENDER_DATA"> 里的 URL 编码 JSON（抖音精选/推荐页用这种） */
  function parseRenderData() {
    const el = document.getElementById('RENDER_DATA');
    if (!el) return null;
    const raw = (el.textContent || '').trim();
    if (!raw) return null;

    // 页面可能给：未编码 JSON / URL 编码 JSON / 再套一层 JSON 字符串 / 带 HTML 实体
    const candidates = [raw];
    try {
      candidates.push(decodeURIComponent(raw));
    } catch {
      /* 不是 URL 编码 */
    }
    // 基础 HTML 实体（&quot; 等）
    candidates.push(
      raw
        .replace(/&quot;/g, '"')
        .replace(/&#34;/g, '"')
        .replace(/&amp;/g, '&')
        .replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>'),
    );
    for (const text of candidates) {
      try {
        let value = JSON.parse(text);
        // 最多再剥 3 层字符串包装
        for (let i = 0; i < 3 && typeof value === 'string'; i += 1) {
          value = JSON.parse(value);
        }
        if (value && typeof value === 'object') return value;
      } catch {
        /* 试下一种 */
      }
    }
    return null;
  }

  //: 明确是图片/封面的特征（排除，避免把封面当媒体）
  const IMAGE_LIKE = /(\.(jpe?g|png|webp|gif|avif|heic)(\?|$))|(image|cover|poster|avatar|thumb)/i;
  //: 疑似媒体的特征（域名 / 路径 / 扩展名）
  //: 明确的媒体地址特征（域 + 路径同时成立才算）
  const MEDIA_PATH = /(\/aweme\/v1\/play|\/video\/tos\/|\/aweme\/v1\/playwm|playwm)/i;
  const MEDIA_HOST = /(douyinvod\.com|snssdk\.com|douyinpic\.com)/i;
  const MEDIA_EXT = /\.(mp4|m4a|mp3|aac|webm|opus)(\?|$)/i;
  //: 明确不是视频内容的路径（客户端安装包、安全/合规页、静态资源等）
  // 注意：不能把 /obj/ 一律排除——抖音的合法音频就放在 /obj/music/ 下
  const NON_MEDIA_PATH = /(\/falcon\/|douyin_pc_client|self_discipline_convention|\/fe_douyin_security|\/eden-cn\/[^/]*\/download)/i;
  const AUDIO_EXT = /\.(m4a|mp3|aac|opus|weba)(\?|$)/i;
  //: 音频扩展（含 m4s），用于最终清洗时判定音频候选
  const AUDIO_EXT_ANY = /\.(m4a|mp3|m4s|aac|opus|weba)(\?|$)/i;

  /**
   * 通用媒体扫描（对键名变化鲁棒）：
   * 不按固定键名找，而是遍历整棵树，收集所有"看起来是媒体"的 URL 字符串，
   * 再按 URL 特征判断音频/视频。用于固定键名失效时兜底。
   */
  /** 严格判定：这个 URL 是否真的是可下载的媒体（而非封面图/安装包/安全页） */
  /** 统一协议：抖音的 JSON 里常出现 //host/... 或 https:\u002F\u002Fhost/... 形式 */
  function normalizeUrl(value) {
    if (typeof value !== 'string') return '';
    let v = value.trim();
    if (v.startsWith('//')) v = 'https:' + v;
    if (/^\/\//.test(v)) v = 'https:' + v;
    return v;
  }

  function isRealMedia(val) {
    if (typeof val !== 'string') return false;
    const v = normalizeUrl(val);
    if (!/^https?:\/\/[^\s"'<>]+$/.test(v)) return false;
    if (IMAGE_LIKE.test(v) || NON_MEDIA_PATH.test(v)) return false;
    return MEDIA_PATH.test(v) || (MEDIA_HOST.test(v) && MEDIA_EXT.test(v));
  }

  function scanForMedia(root) {
    const out = { video: [], audio: [] };
    const seen = new Set();
    const stack = [root];
    let guard = 0;
    while (stack.length && guard < 300000) {
      guard += 1;
      const cur = stack.pop();
      if (typeof cur === 'string') continue;
      if (!cur || typeof cur !== 'object') continue;
      if (Array.isArray(cur)) {
        for (const v of cur) if (v && typeof v === 'object') stack.push(v);
        continue;
      }
      for (const [k, v] of Object.entries(cur)) {
        if (typeof v === 'string') {
          const val = v.trim();
          if (seen.has(val)) continue;
          if (!isRealMedia(val)) continue;
          seen.add(val);
          const keySaysAudio = /music|audio/i.test(k);
          if (AUDIO_EXT.test(val) || keySaysAudio) out.audio.push(val);
          else out.video.push(val);
        } else if (v && typeof v === 'object') {
          stack.push(v);
        }
      }
    }
    return out;
  }

  /** 采集诊断信息：页面上存在哪些数据块 / data-e2e 取值 */
  function collectDiagnostics() {
    const blocks = [];
    for (const id of ['RENDER_DATA', 'SSR_RENDER_DATA', 'SSR_RENDER_DATA_DOC', '__NEXT_DATA__']) {
      const el = document.getElementById(id);
      if (el && (el.textContent || '').length > 10) blocks.push(`${id}(${el.textContent.length})`);
    }
    const scripts = document.scripts || [];
    for (let i = 0; i < scripts.length; i += 1) {
      const text = scripts[i].textContent || '';
      for (const marker of ['_ROUTER_DATA', '__INITIAL_STATE__', 'videoInfoRes', 'aweme_detail']) {
        if (text.includes(marker)) blocks.push(marker);
      }
    }
    const e2e = [];
    try {
      document.querySelectorAll('[data-e2e]').forEach((el) => {
        const v = el.getAttribute('data-e2e');
        if (v && !e2e.includes(v)) e2e.push(v);
      });
    } catch {
      /* 忽略 */
    }
    return { blocks: Array.from(new Set(blocks)).slice(0, 12), e2e: e2e.slice(0, 12) };
  }

  function deepFind(node, keys, objectOnly = false, depth = 0) {
    const out = { value: null, stringFallback: null };
    deepFindInto(node, keys, objectOnly, depth, out);
    return out.value !== null ? out.value : out.stringFallback;
  }

  /**
   * @param out.value          首个命中的对象（objectOnly 模式）
   * @param out.stringFallback 命中的键其值是字符串时的兜底（兼容
   *                           `{videoInfo:{...}}` 与 `{videoInfo:{desc:...}}` 两种结构）
   */
  function deepFindInto(node, keys, objectOnly, depth, out) {
    if (!node || depth > 12) return;
    if (Array.isArray(node)) {
      for (const item of node.slice(0, 8)) {
        deepFindInto(item, keys, objectOnly, depth + 1, out);
        if (out.value !== null) return;
      }
      return;
    }
    if (typeof node !== 'object') return;
    for (const [key, value] of Object.entries(node)) {
      if (!keys.includes(key)) continue;
      if (objectOnly) {
        if (value && typeof value === 'object' && !Array.isArray(value)) {
          out.value = value;
          return;
        }
        // 键命中但值是字符串 → 记录为兜底（例如直接给了 desc）
        if (out.stringFallback === null && typeof value === 'string' && value.trim()) {
          out.stringFallback = node;
        }
      } else if (typeof value === 'string' && value.trim()) {
        out.value = value.trim();
        return;
      }
    }
    for (const value of Object.values(node)) {
      if (value && typeof value === 'object') {
        deepFindInto(value, keys, objectOnly, depth + 1, out);
        if (out.value !== null) return;
      }
    }
  }

  function collectMediaUrls(node, out = [], depth = 0) {
    if (!node || depth > 12) return out;
    if (Array.isArray(node)) {
      node.slice(0, 10).forEach((item) => collectMediaUrls(item, out, depth + 1));
      return out;
    }
    if (typeof node !== 'object') return out;
    for (const [key, value] of Object.entries(node)) {
      if (typeof value === 'string' && /^https?:\/\//.test(value)) {
        if (/\.(mp4|m4a|mp3|webm)(\?|$)/i.test(value) || /(play|media|video|stream)/i.test(key)) {
          out.push(value);
        }
      } else if (value && typeof value === 'object') {
        collectMediaUrls(value, out, depth + 1);
      }
    }
    return out;
  }

  const result = {
    url: location.href,
    host: location.hostname,
    platform: '',
    kind: 'article',
    title: '',
    author: '',
    desc: '',
    text: '',
    mediaUrl: '',
    mediaCandidates: [],
    audioCandidates: [],
    //: 取数来源命中情况（供侧边栏日志展示，便于改版时定位）
    sources: {
      routerData: 0,
      renderData: 0,
      domSelectors: 0,
      domVideo: 0,
      genericScan: 0,
      jsonBlocks: [],
      dataE2E: [],
    },
    mediaDurationSec: 0,
    bvid: '',
    images: [],
    warnings: [],
  };

  try {
    result.build = EXTRACTOR_BUILD;
    result.diagnostics = collectDiagnostics();
    result.title = firstNonEmpty([metaContent(['og:title']), document.title]);
    result.author = metaContent(['author', 'og:article:author']);

    const host = location.hostname;

    // ---------------- 抖音 ----------------
    if (/douyin\.com|iesdouyin\.com/.test(host)) {
      result.platform = '抖音';
      const router = parseAssignedJson('_ROUTER_DATA');

      // --- 抖音精选/推荐页（/jingxuan?modal_id=xxx、/discover、推荐流等）---
      // 这类页面把数据放在 <script id="RENDER_DATA"> 的 URL 编码 JSON 里，
      // 而且字段分散在不同节点（desc 与 playAddr 常常不在一起），
      // 因此这里做整棵树深度聚合，再按 URL 形态区分音视频。
      let aggResult = null;
      const renderData = parseRenderData();
      if (renderData) {
        const agg = { desc: '', title: '', author: '', play: [], audio: [], duration: 0 };
        const seen = new Set();
        const stack = [renderData];
        let guard = 0;
        while (stack.length && guard < 200000) {
          guard += 1;
          const cur = stack.pop();
          if (!cur || typeof cur !== 'object') continue;
          if (Array.isArray(cur)) {
            for (const v of cur) if (v && typeof v === 'object') stack.push(v);
            continue;
          }
          for (const [k, v] of Object.entries(cur)) {
            if (typeof v === 'string') {
              const val = v.trim();
              if (!val) continue;
              if (k === 'desc' && !agg.desc && val.length > 2) agg.desc = val;
              else if (k === 'itemTitle' && !agg.title) agg.title = val;
              else if (k === 'nickname' && !agg.author) agg.author = val;
              else if (/^https?:\/\//.test(val)) {
                const isPlayApi = /aweme\/v1\/play/.test(val);
                const isCdn = /douyinvod|douyinpic|\/video\/tos\//.test(val);
                const looksAudio = /\.(m4a|mp3|aac|opus)(\?|$)/i.test(val) || /music|audio/i.test(k);
                const keyLooksAudio = /music|audio|play.?url/i.test(k) && !/video/i.test(k);
                if ((isPlayApi || isCdn) && !seen.has(val)) {
                  seen.add(val);
                  if (looksAudio || keyLooksAudio) agg.audio.push(val);
                  else agg.play.push(val);
                }
              }
            } else if (v && typeof v === 'object') {
              stack.push(v);
            }
            if (k === 'duration' && typeof v === 'number' && !agg.duration) agg.duration = v;
          }
        }
        // 固定键名没扫到媒体时，用通用扫描兜底（对键名变化鲁棒）
        if (!agg.play.length && !agg.audio.length) {
          const swept = scanForMedia(renderData);
          if (swept.video.length || swept.audio.length) {
            result.sources.genericScan = 1;
            agg.play.push(...swept.video);
            agg.audio.push(...swept.audio);
          }
        }
        result.sources.renderData = agg.desc || agg.title || agg.play.length || agg.audio.length ? 2 : 1;
        // 注意：这里不能直接写 result.desc——下面 DOM/meta 分支会用局部变量覆盖它，
        // 因此先存到 aggResult，最后统一合并（RENDER_DATA 优先，DOM 作为补充）
        aggResult = agg;
        result.audioCandidates.push(...agg.audio);
        result.mediaCandidates.push(...agg.play);
        if (agg.duration && !result.mediaDurationSec) {
          result.mediaDurationSec = agg.duration > 1000 ? agg.duration / 1000 : agg.duration;
        }
      }

      // --- 活动中的视频元素（最贴近真实播放，地址最新）---
      if (!result.mediaUrl) {
        const active = document.querySelector(
          '[data-e2e="feed-active-video"] video, [data-e2e="feed-video"] video, video',
        );
        if (active) {
          const src = active.currentSrc || active.src || '';
          if (/^https?:/.test(src)) {
            result.mediaUrl = src;
            if (Number.isFinite(active.duration) && active.duration > 0) {
              result.mediaDurationSec = active.duration;
            }
          }
        }
      }
      // 只找对象容器，否则会在 videoInfo 内部命中 desc 字符串而提前返回。
      // 已知的容器名：aweme_detail（细节接口）、videoInfo（旧分享页）、
      // videoInfoRes（现分享页，内含 item_list[]）、item_list（兜底）
      let info = deepFind(router, ['aweme_detail'], true) ||
        deepFind(router, ['videoInfo'], true) ||
        deepFind(router, ['videoInfoRes', 'aweme_detail'], true) ||
        deepFind(router, ['item_list'], true);
      // videoInfoRes 的外层不是视频对象本身，真正的内容在 item_list[0]
      if (info && !info.desc && Array.isArray(info.item_list) && info.item_list.length) {
        info = info.item_list[0];
      }
      if (!info) {
        info = deepFind(parseAssignedJson('RENDER_DATA'), ['aweme_detail'], true);
      }
      if (typeof info === 'string') {
        try { info = JSON.parse(info); } catch { info = null; }
      }
      if (router) result.sources.routerData = info ? 2 : 1;
      let desc = '';
      let author = '';
      if (info && typeof info === 'object') {
        desc = String(info.desc || info.item_title || '').trim();
        author = String(info.nickname || info.author?.nickname || '').trim();
        // 优先音频流（体积小得多，且语音识别只需要音频）
        const audioAddr =
          info.music?.play_url?.url_list ||
          info.music?.play_url?.uri ||
          info.video?.play_addr_h264_audio?.url_list;
        if (audioAddr) {
          result.audioCandidates = [].concat(audioAddr).filter((u) => typeof u === 'string');
        }
        // 其次才是视频流
        const playAddr = info.video?.play_addr?.url_list || info.video?.playApi;
        if (playAddr) {
          result.mediaCandidates.push(...[].concat(playAddr).filter((u) => typeof u === 'string'));
        }
        result.mediaDurationSec = Number(info.video?.duration || 0) / 1000 || 0;
      }
      // 固定键名没拿到媒体时，改用通用扫描（对整个 _ROUTER_DATA 找形似媒体的 URL）
      if (!result.mediaCandidates.length && !result.audioCandidates.some(Boolean)) {
        const swept = scanForMedia(router);
        if (swept.video.length || swept.audio.length) {
          result.sources.genericScan = 1;
          result.mediaCandidates.push(...swept.video);
          result.audioCandidates.push(...swept.audio);
        }
      }
      // 优先从已渲染的 DOM 读取（用户真实浏览器里内容是渲染好的，最稳）
      if (!desc) {
        const node = document.querySelector(
          '[data-e2e="video-desc"], [data-e2e="detail-video-desc"], [data-e2e="feed-video-desc"], .video-info-detail',
        );
        const t = textOf(node);
        if (t && !isBoilerplate(t)) {
          desc = t;
          result.sources.domSelectors = 2;
        } else if (node) {
          result.sources.domSelectors = 1;
        }
      }
      if (!author) {
        const node = document.querySelector('[data-e2e="video-author-name"], [data-e2e="user-name"], .account-name');
        const t = textOf(node);
        if (t) author = t.split('·')[0].trim();
      }
      if (!result.title) {
        const node = document.querySelector('[data-e2e="video-desc"]');
        const t = textOf(node);
        if (t) result.title = t.slice(0, 60);
      }
      // 再退到 meta（分享页会带真实简介）
      if (!desc) {
        const meta = metaContent(['og:description', 'description']);
        if (meta && !isBoilerplate(meta)) {
          desc = meta.replace(/\s*[-—]\s*.*?于\d{8}发布在抖音.*$/, '').replace(/\s*来抖音.*$/, '').trim();
        }
      }
      // <video> 元素兜底（用户点过播放时最可靠）
      if (!result.mediaUrl) {
        const v = document.querySelector('video');
        if (v) {
          const src = v.currentSrc || v.src || '';
          if (/^https?:/.test(src)) {
            result.mediaUrl = src;
            result.sources.domVideo = 2;
            if (Number.isFinite(v.duration)) result.mediaDurationSec = v.duration;
          } else {
            result.sources.domVideo = 1;
          }
        }
      }
      // 统一合并：RENDER_DATA（精选页）优先，DOM 兜底
      const agg = aggResult || { desc: '', title: '', author: '' };
      const finalDesc = agg.desc || desc || '';
      const finalTitle = agg.title || result.title || '';
      const finalAuthor = result.author || author || agg.author || '';
      if (finalDesc) result.desc = finalDesc;
      if (finalTitle) result.title = finalTitle;
      if (finalAuthor) result.author = finalAuthor;
      // 统一守卫：不再提前 return，而是交给末尾统一汇总，
      // 这样 hasMedia / sources / mediaUrl 等字段在任何路径下都会被完整赋值。
      // 诊断信息会带上「取数来源 + 页面数据块」，便于改版时定位。
    }

    // ---------------- B 站 ----------------
    else if (/bilibili\.com|b23\.tv/.test(host)) {
      result.platform = 'B站';
      const state = parseAssignedJson('__INITIAL_STATE__');
      // 必须用 videoData（deepFind 会先命中推荐卡片的 desc，是错的）
      const vd = state?.videoData && typeof state.videoData === 'object' ? state.videoData : null;
      if (vd) {
        result.title = String(vd.title || result.title || '').trim();
        const d = String(vd.desc || '').trim();
        result.desc = d === '-' ? '' : d;
        result.author = String(vd.owner?.name || result.author || '').trim();
        if (Number.isFinite(vd.duration)) result.mediaDurationSec = vd.duration;
        if (vd.bvid) result.bvid = vd.bvid;
      }
      if (!result.bvid) {
        const m = location.href.match(/(BV[0-9A-Za-z]{8,12})/);
        if (m) result.bvid = m[1];
      }
      if (!result.bvid) {
        return { error: '未能从 B 站页面识别视频号（BV 号）。请打开具体视频播放页后重试。' };
      }
    }

    // ---------------- 小红书 ----------------
    else if (/xiaohongshu\.com|xhslink\.com/.test(host)) {
      result.platform = '小红书';
      const state = parseAssignedJson('__INITIAL_STATE__');
      let note = null;
      const map = state?.note?.noteDetailMap;
      if (map && typeof map === 'object') {
        note = Object.values(map).map((v) => v?.note).find(Boolean) || null;
      }
      if (!note) note = deepFind(state, ['desc']) ? state : null;
      let desc = '';
      if (note && typeof note === 'object') {
        desc = String(note.desc || '').trim();
        result.title = String(note.title || result.title || '').trim();
        result.author = String(note.user?.nickname || result.author || '').trim();
        result.kind = String(note.type || '').toLowerCase() === 'video' ? 'video' : 'image_text';
        const list = note.imageList || [];
        for (const item of list) {
          const url = item?.urlDefault || item?.urlPre || item?.url ||
            (item?.infoList || []).map((i) => i?.url).find(Boolean);
          if (url) result.images.push(url);
        }
        if (note.video) result.mediaCandidates.push(...collectMediaUrls(note.video));
      }
      if (!desc) {
        const node = document.querySelector('#detail-desc, .desc, .note-content');
        desc = textOf(node);
      }
      result.desc = desc;
      const video = document.querySelector('video');
      if (video?.currentSrc) {
        result.kind = 'video';
        result.mediaUrl = video.currentSrc;
      }
      if (!result.desc && !result.images.length && !result.mediaUrl) {
        return { error: '未能从小红书页面提取到笔记内容。请打开具体笔记详情页（图文或视频），并确认已登录。' };
      }
    }

    // ---------------- 微信公众号 / 通用图文 ----------------
    else {
      if (/mp\.weixin\.qq\.com/.test(host)) result.platform = '微信公众号';
      const contentNode =
        document.querySelector('#js_content') ||
        document.querySelector('article') ||
        document.querySelector('main') ||
        document.body;
      result.text = textOf(contentNode).slice(0, MAX_TEXT);
      result.kind = 'article';
      if (!result.author) {
        result.author = firstNonEmpty([
          textOf(document.querySelector('#js_name, .rich_media_meta_nickname')),
          metaContent(['og:article:author']),
        ]);
      }
      result.desc = metaContent(['og:description', 'description']);
    }

    // ---------------- 通用兜底：页面上的 <video> ----------------
    const video = document.querySelector('video');
    if (video && !result.mediaUrl) {
      const src = video.currentSrc || video.src || '';
      const usable = /^https?:\/\//.test(src) && !/^https?:\/\/[^/]*\/blob:/.test(src);
      if (usable) {
        result.mediaUrl = src;
        if (!result.mediaDurationSec && Number.isFinite(video.duration)) {
          result.mediaDurationSec = video.duration;
        }
      }
      // 即便 <video> 不可用（如 blob: 地址），也要补上文案与时长
      if (!result.desc) {
        result.desc = firstNonEmpty([
          metaContent(['og:description']),
          textOf(document.querySelector('[data-e2e="video-desc"]')),
          result.title,
        ]);
      }
      if (!result.mediaDurationSec && Number.isFinite(video.duration) && video.duration > 0) {
        result.mediaDurationSec = video.duration;
      }
    }

    // 最终清洗：无论候选来自哪条路径（类型路径 / 通用扫描 / 兜底），
    // 都统一用严格媒体判定过一遍，避免封面图、安装包、安全页混进来。
    // 注意不能排除 tos-cn-i —— 那是抖音签名 CDN 的常见前缀。
    result.mediaCandidates = Array.from(
      new Set((result.mediaCandidates || []).map(normalizeUrl).filter((u) => isRealMedia(u))),
    );
    result.audioCandidates = Array.from(
      new Set(
        (result.audioCandidates || [])
          .map(normalizeUrl)
          .filter((u) => /^https?:\/\//.test(u) && !IMAGE_LIKE.test(u) && !NON_MEDIA_PATH.test(u)),
      ),
    );
    if (result.mediaUrl) result.mediaUrl = normalizeUrl(result.mediaUrl);
    // 视频候选排序：直链 CDN > 页面签名的 play 接口（后者只返回跳转，可靠性低）
    const rankMedia = (u) => {
      if (/douyinvod|\/video\/tos\//.test(u)) return 0;
      if (/aweme\/v1\/play\/dash/.test(u)) return 2;
      if (/aweme\/v1\/play/.test(u)) return 3;
      return 1;
    };
    result.mediaCandidates.sort((a, b) => rankMedia(a) - rankMedia(b));
    if (result.mediaUrl) {
      result.mediaCandidates = [result.mediaUrl, ...result.mediaCandidates.filter((u) => u !== result.mediaUrl)];
    }
    // 音频候选去重与清洗：音频允许两种形态——
    //   ① 带音频扩展名（.m4a/.mp3…）
    //   ② 来自 music/audio 字段的地址（抖音常给 /obj/music/... 这类无扩展名链接）
    result.audioCandidates = Array.from(
      new Set(
        (result.audioCandidates || [])
          .filter((u) => typeof u === 'string' && /^https?:\/\//.test(u))
          .filter((u) => !IMAGE_LIKE.test(u) && !NON_MEDIA_PATH.test(u))
          .filter((u) => AUDIO_EXT_ANY.test(u) || MEDIA_PATH.test(u) || /music|audio/i.test(u)),
      ),
    );
    result.audioUrl = result.audioCandidates[0] || '';
    result.hasMedia = Boolean(result.mediaUrl || result.mediaCandidates.length || result.audioUrl);
    // 媒体路径说明，便于日志阅读
    result.mediaVia = result.audioUrl
      ? '音频流'
      : result.mediaUrl
        ? (result.sources.domVideo === 2 ? '<video> 元素' : '页面媒体地址')
        : result.mediaCandidates.length
          ? 'JSON 媒体字段'
          : '';

    if (!result.desc && !result.text && !result.hasMedia && !result.images.length && !result.bvid) {
      const isDouyin = /douyin|iesdouyin/.test(location.hostname);
      const src = result.sources || {};
      const diag = result.diagnostics || {};
      const hint = `（取数来源：_ROUTER_DATA ${src.routerData || 0} / RENDER_DATA ${src.renderData || 0} / DOM ${src.domSelectors || 0} / video ${src.domVideo || 0}`
        + (diag.blocks?.length ? `；数据块：${diag.blocks.join(',')}` : '')
        + (diag.e2e?.length ? `；data-e2e：${diag.e2e.slice(0, 6).join(',')}` : '')
        + '）';
      // 保留已计算好的字段（hasMedia / sources / diagnostics / platform 等），
      // 这样插件在任何失败路径下都能拿到一致的结构用于日志排查
      result.error = isDouyin
        ? `未能从抖音页面读取到视频文案。请确认：① 已登录抖音；② 当前是视频详情页或已打开视频；③ 页面已加载出标题。满足后点「当前页面」重试。${hint}`
        : `当前页面没有可提取的文案或媒体。请打开具体的视频/笔记/文章详情页后重试。${hint}`;
      return result;
    }
    return result;
  } catch (err) {
    return { error: `页面提取出错：${err?.message || err}` };
  }
}

// Node 环境（测试）下导出，供 tests/extension_extractor.test.js 使用
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pageExtractor };
}

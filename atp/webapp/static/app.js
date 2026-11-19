/* ATP 控制台前端：无框架，长轮询驱动的事件流。 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// 虚拟仿真终端已下线：控制台只连真实终端，首次打开默认 Android。
const PLATFORMS = [
  { id: 'android', label: 'Android' },
  { id: 'harmony', label: '鸿蒙' },
  { id: 'desktop', label: '桌面' },
  { id: 'web', label: 'Web' },
];
const MOBILE = ['android', 'harmony'];

const EXAMPLES = [
  '登录阳光 OA，进入待办，打开第一条请假申请，填写同意意见并提交，最后确认审批已提交',
  '启动 App 后用测试账号登录，检查工作台是否展示今日待办数量',
  '在审批详情页不填写意见直接点同意，验证出现「请填写审批意见」提示',
];

const MODE_LABEL = { auto: '自动', ai: 'AI 首跑', replay: '仅回放' };
const TERMINAL = ['passed', 'failed', 'error', 'cancelled'];

const PRESETS = [
  { name: '豆包 · 火山方舟', base: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-seed-1-6-250615' },
  { name: '通义千问', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { name: 'DeepSeek', base: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { name: '智谱 GLM', base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-plus' },
  // 本地部署：都不需要 API Key，且必须直连（自动走 127.0.0.1，不经系统代理）
  { name: '本机 Ollama', base: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b-instruct', local: true },
  { name: '本机 LM Studio', base: 'http://127.0.0.1:1234/v1', model: 'qwen2.5-7b-instruct', local: true },
  { name: '本机 vLLM', base: 'http://127.0.0.1:8000/v1', model: '', local: true },
];

// 与后端 ModelConfig.is_local 同一套口径：本机/局域网算本地
function isLocalBase(url) {
  let host = '';
  try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return false; }
  if (!host) return false;
  if (['localhost', '::1', '0.0.0.0', 'host.docker.internal'].includes(host)) return true;
  const p = host.split('.').map((x) => parseInt(x, 10));
  if (p.length !== 4 || p.some((n) => Number.isNaN(n))) return false;
  return p[0] === 127 || p[0] === 10
    || (p[0] === 192 && p[1] === 168)
    || (p[0] === 172 && p[1] >= 16 && p[1] <= 31);
}

const state = {
  model: null,
  devices: [],
  cases: [],
  platform: 'android',
  mode: 'auto',
  running: false,
  cast: { on: false, manual: false, device: '', timer: null, status: null,
          down: null, actionTimer: null, aspect: '', ratio: 0, pump: 0 },
};

/* ------------------------------------------------------------------ 基础 */
async function api(path, body, method) {
  const opts = body === undefined && !method ? {} : {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text.slice(0, 200) }; }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

const apiDelete = (path) => api(path, undefined, 'DELETE');

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

function fmtMs(ms) {
  if (!ms && ms !== 0) return '';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function fmtAgo(ts) {
  if (!ts) return '';
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

function fmtTime(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

const STATUS_LABEL = { passed: '通过', failed: '未通过', error: '异常', cancelled: '已取消', running: '执行中' };
const STATUS_CLS = { passed: 'ok', failed: 'bad', error: 'bad', cancelled: 'mid', running: 'mid' };

/* ------------------------------------------------------------------ 初始化 */
async function boot() {
  bindEvents();
  renderExamples();
  renderPresets();
  try {
    applyState(await api('/api/state'));
  } catch (e) {
    setPill('err', '服务未连接');
    console.error(e);
  }
}

function applyState(s) {
  state.model = s.model;
  state.devices = s.devices || [];
  state.cases = s.cases || [];
  state.db = s.db || null;
  state.vision = s.vision || {};
  renderModelBadge();
  renderPlatforms();
  renderDevices();
  renderCases();
  renderRuns(s.runs || []);
  const db = state.db || {};
  const counts = db.counts || {};
  $('#tool-line').textContent =
    `adb ${s.toolchain?.adb ? '✓' : '—'} · hdc ${s.toolchain?.hdc ? '✓' : '—'} · ` +
    `${state.devices.length} 终端 · 库 ${counts.cases ?? 0} 用例 / ${counts.runs ?? 0} 次`;
  // 库的位置和每张表的行数直接挂在 tooltip 上——"数据到底存哪了"是必答题
  $('#tool-line').title = db.path
    ? `SQLite: ${db.path}\n` + (db.tables || []).map(t => `${t.name}(${t.label}) ${t.rows} 行`).join('\n')
    : '用例库不可用，已降级为扫描 cases/*.stepir.json';
}

/* ------------------------------------------------------------------ 侧栏 */
function renderModelBadge() {
  const m = state.model || {};
  const dot = $('#model-dot');
  const txt = $('#model-text');
  if (m.configured) {
    dot.className = 'ok';
    txt.textContent = m.model;
    txt.title = `${m.model} @ ${m.base_url}`;
  } else {
    dot.className = 'warn';
    txt.textContent = '模型未配置 · 请到模型设置填写';
  }
}

function renderPlatforms() {
  const box = $('#platforms');
  if (!box) return;   // 左栏「执行终端」已移除；平台切换入口在输入框的 #exec-device
  box.innerHTML = '';
  for (const p of PLATFORMS) {
    const n = state.devices.filter((d) => d.platform === p.id).length;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = p.label;
    btn.dataset.platform = p.id;
    if (p.id === state.platform) btn.classList.add('on');
    if (!n && p.id !== state.platform) btn.classList.add('off');
    btn.title = n ? `${n} 个可用终端` : '未发现终端';
    btn.onclick = () => {
      if (state.platform === p.id) return;
      state.platform = p.id;
      // 换终端后旧的投屏连接指向别的设备，先断掉；下次执行会自动重开
      if (state.cast.on) stopCast();
      renderPlatforms();
      renderDevices();
    };
    box.appendChild(btn);
  }
}

function renderDevices() {
  const sel = $('#device');
  const list = state.devices.filter((d) => d.platform === state.platform);
  if (sel) {             // 左栏设备下拉已移除；粒度选择用 #exec-device 菜单 + 投屏面板
    sel.innerHTML = '';
    const auto = document.createElement('option');
    auto.value = '';
    auto.textContent = list.length ? '自动选择最佳设备' : '未发现设备（自动调度）';
    sel.appendChild(auto);
    for (const d of list) {
      const o = document.createElement('option');
      o.value = d.device_id;
      o.textContent = `${d.device_id}${d.kind === 'remote' ? ' · 远程' : ''}`;
      sel.appendChild(o);
    }
  }
  const hint = $('#dev-hint');
  if (hint) hint.textContent = list.length
    ? `${list.length} 个终端可用 · 移动端执行时右侧实时投屏`
    : '本地未发现设备，插上真机后点右侧刷新按钮';
  // 输入框内的设备按钮跟着平台选择走（与左栏 #platforms 共享同一 state）
  const label = $('#exec-device-label');
  if (label) {
    const p = PLATFORMS.find((x) => x.id === state.platform);
    const n = list.length;
    label.textContent = p ? p.label : state.platform;
    $('#exec-device').title = n
      ? `${p ? p.label : state.platform} · ${n} 个可用终端（点击切换）`
      : '未发现终端（点击切换平台）';
  }
  renderCastPanel();
}

// 输入框内的设备下拉：列出四端，选中后复用左栏同一套切换逻辑
function openExecDeviceMenu(anchor) {
  closeExecDeviceMenu();
  const menu = document.createElement('div');
  menu.className = 'exec-device-menu';
  menu.id = 'exec-device-menu';
  for (const p of PLATFORMS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.platform = p.id;
    const n = state.devices.filter((d) => d.platform === p.id).length;
    b.innerHTML = `<span>${p.label}</span><em>${n ? n + ' 台' : '无设备'}</em>`;
    if (p.id === state.platform) b.classList.add('on');
    b.onclick = () => {
      if (state.platform !== p.id) {
        state.platform = p.id;
        if (state.cast.on) stopCast();
        renderPlatforms();
        renderDevices();
      }
      closeExecDeviceMenu();
    };
    menu.appendChild(b);
  }
  // 刷新设备：左栏刷新按钮已随「执行终端」移除，入口收进这个菜单
  const rf = document.createElement('button');
  rf.type = 'button';
  rf.className = 'refresh';
  rf.innerHTML = '<span>↻ 重新扫描设备</span>';
  rf.onclick = async (ev) => {
    ev.stopPropagation();
    closeExecDeviceMenu();
    await refreshDevices();
  };
  menu.appendChild(rf);
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.round(r.top - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.round(r.left)}px`;
  // 点空白处关闭
  setTimeout(() => document.addEventListener('click', onDocCloseExecMenu), 0);
}

function closeExecDeviceMenu() {
  const m = $('#exec-device-menu');
  if (m) m.remove();
  document.removeEventListener('click', onDocCloseExecMenu);
}
function onDocCloseExecMenu(e) {
  if (!e.target.closest('#exec-device') && !e.target.closest('#exec-device-menu')) {
    closeExecDeviceMenu();
  }
}

/* ------------------------------------------------ 右侧设备面板 / 实时投屏 */
const BLANK_IMG = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

function castSupported() {
  return MOBILE.includes(state.platform);
}

function castDevice() {
  const picked = $('#device')?.value || '';
  if (picked) return picked;
  const list = state.devices.filter((d) => d.platform === state.platform);
  return list.length ? list[0].device_id : '';
}

function castQuery() {
  return `platform=${encodeURIComponent(state.platform)}&device=${encodeURIComponent(state.cast.device)}`;
}

const DR_MIN_W = 240;
const DR_MAX_W = 560;

function mobileDevices() {
  return state.devices.filter((d) => MOBILE.includes(d.platform));
}

function platformLabel(id) {
  return id === 'harmony' ? 'harmony' : 'Android';
}

/* 未连接态只保留：设备序列号 / 设备类型 / 连接 */
async function refreshDevices() {
  try {
    const d = await api('/api/devices');
    state.devices = d.devices || [];
    renderPlatforms();
    renderDevices();
    // 设备还在就不要重启流：重启会让画面闪一下，看起来像抖动
    const cur = state.cast.device;
    if (state.cast.on && cur && !state.devices.some((x) => x.device_id === cur)) stopCast();
    else renderCastPanel();
    toast('设备列表已刷新');
  } catch (e) {
    toast(`刷新失败：${e.message}`, 'err');
  }
}

function renderDeviceList() {
  const box = $('#dr-list');
  box.innerHTML = '';
  const list = mobileDevices();
  if (!list.length) {
    box.innerHTML = '<div class="dr-empty">未发现 Android / harmony 真机。</div>'
      + '<button class="dr-empty-btn" type="button">刷新设备</button>';
    box.querySelector('.dr-empty-btn').onclick = () => refreshDevices();
    return;
  }
  for (const d of list) {
    const row = document.createElement('div');
    row.className = 'dr-item';
    row.innerHTML = `<b title="${esc(d.device_id)}">${esc(d.device_id)}</b>`
      + `<span class="dr-tag">${platformLabel(d.platform)}</span>`
      + '<button class="ghost-btn" type="button">连接</button>';
    row.querySelector('button').onclick = () => connectDevice(d.device_id, d.platform);
    box.appendChild(row);
  }
}

function renderCastPanel() {
  const on = state.cast.on;
  $('#dr-idle').classList.toggle('hidden', on);
  $('#dr-live').classList.toggle('hidden', !on);
  $('#dr-sub').textContent = on ? '已连接 · 实时画面' : '未连接设备';

  const dot = $('#cast-dot');
  dot.className = 'dr-dot' + (state.cast.status?.error ? ' err' : (on ? ' on' : ''));
  dot.title = state.cast.status?.error || (on ? '已连接' : '未连接');

  if (!on) {
    renderDeviceList();
    $('#cast-live').classList.add('hidden');
    return;
  }
  $('#cast-device').textContent = state.cast.device || '—';
  $('#cast-platform').textContent = platformLabel(state.platform);
  $('#cast-toggle').textContent = '断开连接';
  $('#cast-live').classList.remove('hidden');
  fitCastStage();
}

function connectDevice(deviceId, platform) {
  if (!MOBILE.includes(platform)) { toast('该终端不支持投屏'); return; }
  if (state.platform !== platform) {
    state.platform = platform;
    renderPlatforms();
  }
  renderDevices();                       // 重建左栏下拉（内部会重绘面板，无副作用）
  const sel = $('#device');
  if (sel) sel.value = deviceId;         // 直接赋值，不触发 change，避免重复开关流
  startCast(true, deviceId);
}

/* 投屏画面用「客户端拉取 + 长轮询」而不是 MJPEG 长连接。
   MJPEG 是服务端往浏览器里灌帧，浏览器解码一旦跟不上，帧就在浏览器内部排队，
   开始回放几秒前的画面——真机上表现为：用户已经点回消息首页，面板却还在
   重播刚才那几页（聊天详情 → 通讯录 → 我的）来回闪。
   拉取模式下：取一帧 → 等它解码完 → 再带 after=这一帧序号取下一帧，
   服务端没有更新时会把请求挂起，所以节奏天然等于发布帧率（后端已限 30fps），
   结构上不可能积压；客户端慢只会掉帧率，画面时间线绝不会倒退。
   注意：这里**不要**再加客户端 sleep 节流，那会在长轮询的等待之上再叠一层，
   把 30fps 直接砍到 8~15fps（真机实测过）。 */

function sleepMs(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/* 视频模式：screenrecord H264 → WebCodecs 硬解 → canvas。
   这是首选路径：canvas.drawImage 原子渲染，不存在换 src 闪烁，
   也不存在 JPEG 积压回放；PC 端零解码，浏览器硬解 60fps 毫无压力。
   线协议：每条记录 = 4 字节大端长度 + 一个 NAL（不含起始码）。
   浏览器把 SPS/PPS 当配置，从首个 IDR 开始解码。 */
const ANNEXB = new Uint8Array([0, 0, 0, 1]);

function nalType(u8) {
  return u8.length ? (u8[0] & 0x1f) : -1;
}

function codecFromSps(sps) {
  // SPS payload 的第 1~3 字节 = profile_idc / 约束标志 / level_idc
  if (sps.length < 4) return null;
  const hex = (v) => v.toString(16).padStart(2, '0');
  return `avc1.${hex(sps[1])}${hex(sps[2])}${hex(sps[3])}`;
}

/* 海报帧：screenrecord 在画面静止时连 SPS 都不发（编码器只在内容变化时出帧），
   所以刚连上如果屏幕是静止的，视频流会是空的。先抓一张 JPEG 快照画上 canvas
   当第一帧，视频流一到就无缝接管 —— 用户看到的是 0.3s 内出静态图 + 实时视频。 */
async function showPosterFrame() {
  const canvas = $('#cast-canvas');
  const ctx = canvas.getContext('2d');
  try {
    const resp = await fetch(`/api/device/cast/frame?${castQuery()}`, { cache: 'no-store' });
    if (!resp.ok) return;
    const bmp = await createImageBitmap(await resp.blob());
    canvas.width = bmp.width;
    canvas.height = bmp.height;
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close();
    if (state.cast.on && state.cast.videoMode) {
      canvas.classList.remove('hidden');   // .hidden 是 display:none !important，不移除画面永远出不来
      canvas.classList.add('on');
    }
  } catch { /* 抓不到就算了，等视频帧 */ }
}

function startVideoPump() {
  stopFramePump();               // 先确保旧的拉帧循环彻底退出，绝不并发
  state.cast.pump += 1;
  const token = state.cast.pump;
  const canvas = $('#cast-canvas');
  const img = $('#cast-img');
  const ctx = canvas.getContext('2d');
  const ctl = new AbortController();
  state.cast.abort = ctl;
  let decoder = null;
  let codecStr = null;
  let spsKey = '';
  let needKey = true;
  let lastSeq = -1;
  let ts = 0;
  let prefixNals = [];        // SPS/PPS/SEI 前缀，凑到 slice 才构成一个完整帧

  const draw = (frame) => {
    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
      canvas.width = frame.displayWidth;
      canvas.height = frame.displayHeight;
      syncStageSize(frame.displayWidth, frame.displayHeight);
    }
    ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
    frame.close();
    canvas.classList.remove('hidden');    // 同上：不移除 .hidden 画面永远 display:none
    canvas.classList.add('on');
    img.classList.remove('on');       // 视频模式接管，隐藏 JPEG 回退层
  };

  const ensureDecoder = () => {
    if (!codecStr) return;
    if (decoder && decoder.state === 'configured') return;
    if (!decoder || decoder.state === 'closed') {
      decoder = new VideoDecoder({
        output: (frame) => {
          try { draw(frame); } catch { /* canvas 尺寸异常就丢弃这一帧 */ }
        },
        error: () => { needKey = true; },   // 解码出错：等下一个 IDR 重新配
      });
    }
    if (decoder.state === 'unconfigured') {
      decoder.configure({ codec: codecStr, optimizeForLatency: true });
    }
  };

  const feedNals = (nals) => {
    // NAL 分组：SPS/PPS/SEI 是前缀，遇到 slice(1/5) 就构成一个完整的帧。
    // prefixNals 存在闭包里：SPS 可能和它的 slice 不在同一次 fetch 里。
    for (const nal of nals) {
      const t = nalType(nal);
      if (t === 7 || t === 8 || t === 6) { prefixNals.push(nal); continue; }
      if (t !== 1 && t !== 5) continue;
      const isKey = t === 5;
      if (needKey && !isKey) continue;         // 等关键帧，之前的都解不了
      if (decoder && decoder.decodeQueueSize > 60) {
        decoder.reset();                        // 真跟不上：丢到下一个 IDR 重来
        needKey = true;
        continue;
      }
      ensureDecoder();
      const parts = [];
      for (const p of prefixNals) parts.push(ANNEXB, p);
      parts.push(ANNEXB, nal);
      prefixNals = [];
      let len = 0;
      for (const p of parts) len += p.length;
      const data = new Uint8Array(len);
      let off = 0;
      for (const p of parts) { data.set(p, off); off += p.length; }
      ts += 33333;
      try {
        decoder.decode(new EncodedVideoChunk({
          type: isKey ? 'key' : 'delta',
          timestamp: ts,
          data,
        }));
        needKey = false;
      } catch { needKey = true; }
    }
  };

  (async () => {
    while (state.cast.on && state.cast.pump === token) {
      let resp;
      try {
        resp = await fetch(`/api/device/cast/video?${castQuery()}`,
                           { signal: ctl.signal, cache: 'no-store' });
      } catch {
        if (state.cast.pump !== token) return;
        await sleepMs(300);
        continue;
      }
      if (!resp.ok || !resp.body) { await sleepMs(300); continue; }
      const reader = resp.body.getReader();
      let buf = new Uint8Array(0);
      let view = new DataView(buf.buffer);
      while (state.cast.on && state.cast.pump === token) {
        let chunk;
        try {
          const r = await reader.read();
          if (r.done) break;
          chunk = r.value;
        } catch {
          break;                          // 断开：外层重连
        }
        // 拼包
        const merged = new Uint8Array(buf.length + chunk.length);
        merged.set(buf, 0);
        merged.set(chunk, buf.length);
        buf = merged;
        view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
        let off = 0;
        const nals = [];
        while (off + 4 <= buf.length) {
          const len = view.getUint32(off);
          if (off + 4 + len > buf.length) break;
          nals.push(buf.subarray(off + 4, off + 4 + len));
          off += 4 + len;
        }
        buf = buf.slice(off);
        if (!nals.length) continue;
        // SPS 变化 → 重配解码器
        const sps = nals.find((n) => nalType(n) === 7);
        if (sps) {
          const key = sps.join(',');
          if (key !== spsKey) {
            spsKey = key;
            codecStr = codecFromSps(sps);
            if (decoder) { try { decoder.close(); } catch { /* 已关闭 */ } decoder = null; }
            needKey = true;
          }
        }
        feedNals(nals);
      }
      // 流结束（time-limit 重启/设备拔出）：外层重连
      if (decoder) { try { decoder.close(); } catch { /* noop */ } decoder = null; }
      if (state.cast.on && state.cast.pump === token) await sleepMs(120);
    }
  })();
}

function stopFramePump() {
  state.cast.pump += 1;         // 让在跑的循环自己退出
  if (state.cast.abort) {
    try { state.cast.abort.abort(); } catch { /* noop */ }
    state.cast.abort = null;
  }
  const c = $('#cast-canvas');
  c.classList.remove('on');
  c.classList.add('hidden');
}

function startFramePump() {
  stopFramePump();               // 先确保旧的拉帧循环彻底退出，绝不并发
  state.cast.pump += 1;
  const token = state.cast.pump;
  const img = $('#cast-img');
  (async () => {
    let lastSeq = 0;
    while (state.cast.on && state.cast.pump === token) {
      let resp;
      try {
        // after=手上这一帧的序号：没有更新时服务端会挂起 1.5s 再回 204，
        // 所以静止画面几乎不产生流量，也不用客户端空转轮询。
        resp = await fetch(`/api/device/cast/latest?${castQuery()}&after=${lastSeq}`,
                           { cache: 'no-store' });
      } catch {
        await sleepMs(150);     // 控制台重启/网络抖动：等一会儿再试
        continue;
      }
      if (resp.status === 204) {
        const s = Number(resp.headers.get('X-Frame-Seq'));
        if (Number.isFinite(s) && s >= 0) lastSeq = s;
        if (state.cast.pump !== token) return;
        continue;
      }
      if (!resp.ok) { await sleepMs(150); continue; }
      const seq = Number(resp.headers.get('X-Frame-Seq') || 0);
      let blob;
      try {
        blob = await resp.blob();
      } catch {
        await sleepMs(150);
        continue;
      }
      if (state.cast.pump !== token) return;
      if (!seq) { await sleepMs(50); continue; }   // 后端没给序号就别空转
      if (seq === lastSeq) continue;   // 防御：同一帧不重复绘制
      lastSeq = seq;
      const url = URL.createObjectURL(blob);
      // 先等**这张**新图解码就绪再上屏，上屏之后才吊销上一张的 URL。
      // 顺序反过来（先换 src 再 revoke）会在浏览器还没消费完旧 URL 时就把它
      // 吊销，导致旧帧闪一下空白 —— 这是「画面闪烁」的常见来源之一。
      await new Promise((resolve) => {
        const probe = new Image();
        probe.onload = () => {
          if (state.cast.pump !== token) { URL.revokeObjectURL(url); resolve(); return; }
          const prev = img.dataset.blob || '';
          img.src = url;
          img.dataset.blob = url;
          img.classList.add('on');
          if (prev) URL.revokeObjectURL(prev);   // 新图已上屏，旧 URL 安全释放
          resolve();
        };
        probe.onerror = () => { URL.revokeObjectURL(url); resolve(); };
        probe.src = url;
      });
    }
  })();
}

function startCast(manual = false, forcedDevice = '') {
  if (!castSupported()) { toast('投屏仅支持 Android / 鸿蒙真机'); return; }
  const did = forcedDevice || castDevice();
  if (!did) { renderCastPanel(); toast('未发现真机，请先连接设备'); return; }
  // 幂等：同一台设备已经在投屏就别重开——重开会叠加第二个拉帧循环，
  // 两个循环往同一个画面元素交错写，画面就在旧页新页之间来回跳。
  if (state.cast.on && state.cast.device === did) {
    renderCastPanel();
    return;
  }
  if (state.cast.on) stopCast();   // 换了设备：先干净收掉旧的
  state.cast.device = did;
  if (manual) state.cast.manual = true;
  state.cast.on = true;
  state.cast.status = null;
  // 视频模式（screenrecord H264 → WebCodecs）只有 Android 有这条通道：
  // 鸿蒙的 snapshot_display 产不出 H264 流，硬走视频模式画面会冻在海报帧
  // （真机复现：鸿蒙连上后投屏永不刷新）。鸿蒙一律走 JPEG 拉取链路。
  state.cast.videoMode = state.platform === 'android'
                         && typeof VideoDecoder !== 'undefined'
                         && typeof EncodedVideoChunk !== 'undefined';
  if (state.cast.videoMode) {
    $('#cast-img').classList.remove('on');
    showPosterFrame();               // 静止画面时视频流是静默的，先给一张快照
    startVideoPump();
  } else {
    startFramePump();              // 老浏览器没有 WebCodecs：退回 JPEG 拉取
  }
  renderCastPanel();
  startCastPolling();
}

function stopCast() {
  const img = $('#cast-img');
  img.classList.remove('on');
  const canvas = $('#cast-canvas');
  canvas.classList.remove('on');
  canvas.classList.add('hidden');
  stopFramePump();
  if (img.dataset.blob) {         // 真正断开时回收当前帧的 URL
    URL.revokeObjectURL(img.dataset.blob);
    img.dataset.blob = '';
  }
  img.src = BLANK_IMG;          // 换回 1x1 空图，别把最后一帧留在面板上
  state.cast.on = false;
  state.cast.manual = false;
  if (state.cast.timer) { clearInterval(state.cast.timer); state.cast.timer = null; }
  if (state.cast.device) {
    api('/api/device/cast/stop', { platform: state.platform, device: state.cast.device })
      .catch(() => {});
  }
  $('#cast-fps').textContent = '— fps';
  $('#cast-method').textContent = '—';
  $('#cast-action').classList.add('hidden');
  clearTimeout(state.cast.actionTimer);
  $('#cast-size').textContent = '—';
  $('#cast-time').textContent = '未连接';
  state.cast.down = null;
  $('#cast-error').classList.add('hidden');
  $('#cast-error').textContent = '';
  state.cast.status = null;
  renderCastPanel();
}

function startCastPolling() {
  if (state.cast.timer) clearInterval(state.cast.timer);
  const tick = async () => {
    if (!state.cast.on) return;
    try {
      const st = await api(`/api/device/cast?${castQuery()}`);
      state.cast.status = st;
      // 设备被拔线/掉线：服务端探测（3s 缓存）返回 present=false。
      // 立刻断开面板并刷新设备列表，不能让画面永远停在最后一帧。
      if (st.present === false) {
        toast('设备已断开连接（USB 已移除或设备离线）', 'err');
        stopCast();
        refreshDevices();
        return;
      }
      const methods = {
        'h264+': 'H264 加速', h264: 'H264 实时流',
        javacap: 'javacap 实时帧', adbcap: 'adb 截图',
      };
      $('#cast-method').textContent = st.method ? (methods[st.method] || st.method) : '连接中…';
      $('#cast-fps').textContent = st.active
        ? `${st.fps || st.target_fps} fps${st.frame_ms ? ` · ${st.frame_ms}ms` : ''}` : '连接中…';
      $('#cast-size').textContent = st.width ? `${st.width}×${st.height}` : '—';
      $('#cast-time').textContent = st.updated_at ? fmtTime(st.updated_at) : '未连接';
      $('#cast-error').textContent = st.error || '';
      $('#cast-error').classList.toggle('hidden', !st.error);
      if (!state.cast.videoMode && Array.isArray(st.frame_size) && st.frame_size[0] > 0) {
        // 视频模式的画面比例由解码帧自带尺寸驱动（draw 里 syncStageSize），
        // 这里若用 JPEG 通道的旧尺寸会把 stage 比例改错。
        syncStageSize(st.frame_size[0], st.frame_size[1]);
      }
      const dot = $('#cast-dot');
      dot.className = 'dr-dot' + (st.error ? ' err' : (st.active ? ' on' : ' busy'));
      dot.title = st.error || (st.active ? '投屏中' : '连接中');
    } catch (e) {
      console.error(e);
    }
  };
  tick();
  state.cast.timer = setInterval(tick, 1500);
}

/* 画面几何只由「面板宽度 × 设备宽高比」决定，不随状态文字/反馈出现而变化 */
function syncStageSize(w, h) {
  if (!w || !h) return;
  const key = `${w}x${h}`;
  if (state.cast.aspect === key) return;
  state.cast.aspect = key;
  state.cast.ratio = h / w;
  fitCastStage();
}

function maxCastHeight() {
  return Math.max(220, window.innerHeight - 200);
}

/* 拖动上限按「画面还能等比铺满 stage」反推：再宽就会因视口高度截断而
   在左右留黑边，看起来像画面没跟着拖动走。 */
function maxPanelWidth() {
  const ratio = state.cast.ratio || 924 / 416;
  return Math.min(DR_MAX_W, Math.round(maxCastHeight() / ratio + 32));
}

function clampPanelWidth(w) {
  return Math.min(maxPanelWidth(), Math.max(DR_MIN_W, w));
}

function fitCastStage() {
  const stage = $('#cast-stage');
  if (!stage || !stage.clientWidth) return;
  const ratio = state.cast.ratio || 924 / 416;
  stage.style.height = `${Math.round(Math.min(stage.clientWidth * ratio, maxCastHeight()))}px`;
}

function initPanelResize() {
  const saved = Number(localStorage.getItem('atp.dr.width') || 0);
  if (saved >= DR_MIN_W && saved <= DR_MAX_W) {
    document.documentElement.style.setProperty('--dr-w', `${clampPanelWidth(saved)}px`);
  }
  const grip = $('#dr-grip');
  let startX = 0, startW = 0, dragging = false, raf = 0;
  grip.addEventListener('pointerdown', (e) => {
    dragging = true;
    startX = e.clientX;
    startW = $('#device-rail').getBoundingClientRect().width;
    try { grip.setPointerCapture(e.pointerId); } catch { /* noop */ }
    grip.classList.add('dragging');
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    // 手柄在左边缘：往左拖 = 面板变宽
    const w = clampPanelWidth(startW + (startX - e.clientX));
    document.documentElement.style.setProperty('--dr-w', `${w}px`);
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; fitCastStage(); });
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('dragging');
    const w = Math.round($('#device-rail').getBoundingClientRect().width);
    localStorage.setItem('atp.dr.width', String(w));
    fitCastStage();
  };
  grip.addEventListener('pointerup', end);
  grip.addEventListener('pointercancel', end);
  window.addEventListener('resize', () => {
    const w = clampPanelWidth($('#device-rail').getBoundingClientRect().width);
    document.documentElement.style.setProperty('--dr-w', `${w}px`);
    fitCastStage();
  });
}

/* ------------------------------------------------ 投屏画面直接操作真机 */
// object-fit: contain 会在图片两侧/上下留黑边，必须按"实际画出来的那块矩形"
// 归一化，否则缩放比例一变点击就偏。坐标一律 0~1，由后端乘设备物理分辨率。
// 视频模式画在 canvas 上（位图比例与 stage 一致，直接全幅映射）。
function castPoint(ev) {
  const canvas = $('#cast-canvas');
  if (canvas.classList.contains('on') && canvas.width) {
    const box = canvas.getBoundingClientRect();
    const x = (ev.clientX - box.left) / box.width;
    const y = (ev.clientY - box.top) / box.height;
    return (x >= 0 && x <= 1 && y >= 0 && y <= 1) ? { x, y } : null;
  }
  const img = $('#cast-img');
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const box = img.getBoundingClientRect();
  const scale = Math.min(box.width / nw, box.height / nh);
  const dw = nw * scale, dh = nh * scale;
  const left = box.left + (box.width - dw) / 2;
  const top = box.top + (box.height - dh) / 2;
  const x = (ev.clientX - left) / dw;
  const y = (ev.clientY - top) / dh;
  return (x >= 0 && x <= 1 && y >= 0 && y <= 1) ? { x, y } : null;
}

function castRipple(clientX, clientY) {
  const el = $('#cast-ripple');
  const box = $('#cast-stage').getBoundingClientRect();
  el.style.left = `${clientX - box.left}px`;
  el.style.top = `${clientY - box.top}px`;
  el.classList.remove('hit');
  void el.offsetWidth;              // 重启动画
  el.classList.add('hit');
}

async function castSend(payload) {
  if (!state.cast.on || !state.cast.device) { toast('请先开始投屏'); return; }
  try {
    const r = await api('/api/device/cast/input', {
      platform: state.platform, device: state.cast.device, ...payload,
    });
    // 单独一格显示操作反馈：状态轮询每 1.5s 会刷新 cast-time，别把回显冲掉
    const act = $('#cast-action');
    act.textContent = `${payload.action} · ${r.latency_ms}ms`;
    act.classList.remove('hidden', 'err');
    clearTimeout(state.cast.actionTimer);
    state.cast.actionTimer = setTimeout(() => act.classList.add('hidden'), 4000);
  } catch (e) {
    const act = $('#cast-action');
    act.textContent = `操作失败 · ${e.message}`;
    act.classList.remove('hidden');
    act.classList.add('err');
    toast(`设备操作失败：${e.message}`, 'err');
  }
}

function initCastControls() {
  // 视频模式画在 canvas 上、JPEG 回退画在 img 上：两个元素都要能点。
  const surfaces = [$('#cast-img'), $('#cast-canvas')];
  for (const img of surfaces) {
  img.addEventListener('pointerdown', (ev) => {
    if (!state.cast.on || ev.button !== 0) return;
    const p = castPoint(ev);
    if (!p) return;
    state.cast.down = { ...p, t: Date.now() };
    try { img.setPointerCapture(ev.pointerId); } catch { /* 某些浏览器不支持 */ }
    castRipple(ev.clientX, ev.clientY);
    ev.preventDefault();
  });
  img.addEventListener('pointerup', (ev) => {
    const start = state.cast.down;
    if (!start) return;
    state.cast.down = null;
    const p = castPoint(ev) || start;
    const dt = Date.now() - start.t;
    const dist = Math.hypot(p.x - start.x, p.y - start.y);
    if (dist < 0.012 && dt < 500) {
      castSend({ action: 'tap', x: start.x, y: start.y });
    } else if (dist < 0.012) {
      castSend({ action: 'long_press', x: start.x, y: start.y, duration: Math.min(2000, dt) });
    } else {
      castSend({
        action: 'swipe', x: start.x, y: start.y, x2: p.x, y2: p.y,
        duration: Math.max(120, Math.min(1200, dt)),
      });
    }
    ev.preventDefault();
  });
  img.addEventListener('pointercancel', () => { state.cast.down = null; });
  }

  // 画面内部的系统导航键；阻止冒泡，避免被当成画面点击
  $$('.cast-nav button').forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      castSend({ action: 'key', key: b.dataset.key });
    };
  });
}

function renderCases() {
  const box = $('#case-list');
  box.innerHTML = '';
  $('#case-count').textContent = state.cases.length;
  if (!state.cases.length) {
    box.innerHTML = '<div class="rail-empty">还没有沉淀用例。<br>先在右侧用「AI 首跑」跑通一条，会自动生成 StepIR 与可执行脚本。</div>';
    return;
  }
  for (const c of state.cases) {
    const item = document.createElement('div');
    item.className = 'case-item';
    item.dataset.caseId = c.case_id;
    item.innerHTML = `
      <div class="ci-top">
        <span class="ci-title" title="${esc(c.title)}">${esc(c.title)}</span>
        <span class="ci-steps">${c.steps} 步</span>
        <button class="ci-more" type="button" title="更多操作">···</button>
      </div>
      <div class="ci-sub"><b>${esc(c.platform)}</b><span>${fmtAgo(c.updated_at || c.created_at)}</span>${c.has_script ? '<span>已编译</span>' : ''}${c.images ? `<span title="这些步骤靠截图模板定位，换分辨率也能点到">${c.images} 图像步</span>` : ''}${c.hit_count ? `<span>命中 ${c.hit_count}</span>` : ''}${c.last_status ? `<span class="ci-state ${STATUS_CLS[c.last_status] || ''}">${STATUS_LABEL[c.last_status] || esc(c.last_status)}</span>` : ''}${c.screen && c.screen !== '0x0' ? `<span>${esc(c.screen)}</span>` : ''}</div>
      <span class="ci-go">查看执行过程</span>`;
    // 单击 = 看这条用例跑过什么（不再"点一下就直接跑"：误触一次就白等一分钟）
    item.onclick = () => openCaseDetail(c);
    item.querySelector('.ci-more').onclick = (e) => {
      e.stopPropagation();
      openCaseMenu(e.currentTarget, c);
    };
    box.appendChild(item);
  }
}

/* ------------------------------------------------------------------ 用例操作菜单 */
let ctxEl = null;

function closeCaseMenu() {
  if (ctxEl) { ctxEl.remove(); ctxEl = null; }
}

function openCaseMenu(anchor, c) {
  closeCaseMenu();
  const rect = anchor.getBoundingClientRect();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.innerHTML = `
    <button type="button" data-act="detail">查看执行过程</button>
    <button type="button" data-act="replay">回放这条用例</button>
    <button type="button" data-act="delete" class="danger">删除用例…</button>`;
  document.body.appendChild(menu);
  const w = menu.offsetWidth || 150;
  menu.style.top = `${Math.round(rect.bottom + 5)}px`;
  menu.style.left = `${Math.round(Math.max(8, Math.min(rect.right - w, window.innerWidth - w - 10)))}px`;
  menu.querySelectorAll('button').forEach((b) => {
    b.onclick = (ev) => {
      ev.stopPropagation();
      const act = b.dataset.act;
      closeCaseMenu();
      if (act === 'detail') openCaseDetail(c);
      else if (act === 'replay') replayCase(c);
      else if (act === 'delete') deleteCase(c);
    };
  });
  ctxEl = menu;
}

function replayCase(c) {
  closeCaseDetail();
  $('#nl').value = c.nl || c.title;
  autoGrow();
  send({ text: c.nl || c.title, case_id: c.case_id, mode: 'replay', title: c.title });
}

async function deleteCase(c) {
  const ok = window.confirm(
    `删除用例「${c.title}」？\n\n` +
    `会同时删除：\n` +
    `· 用例库记录（用例 / 步骤 / 视觉模板索引）\n` +
    `· ${c.hit_count || 0} 次命中统计与执行记录\n` +
    `· 脚本 .py / .stepir.json 与截图产物\n\n` +
    `删除后不可恢复。`
  );
  if (!ok) return;
  try {
    const r = await apiDelete(`/api/cases/${encodeURIComponent(c.case_id)}`);
    closeCaseDetail();
    toast(`已删除「${r.title || c.title}」：执行记录 ${r.db?.runs || 0} 条，文件 ${r.files?.length || 0} 项`);
    await refreshSide();
  } catch (e) {
    toast(`删除失败：${e.message}`, 'err');
  }
}

/* ------------------------------------------------------------------ 用例执行过程 */
let cmCase = null;

async function openCaseDetail(c) {
  cmCase = c;
  $('#cm-title').textContent = c.title;
  $('#cm-sub').textContent = `${c.platform} · ${c.steps} 步 · 命中 ${c.hit_count || 0} 次`;
  $('#cm-body').innerHTML = '<div class="cm-loading">正在读取执行过程…</div>';
  $('#case-modal').classList.add('on');
  $('#case-mask').classList.add('on');
  try {
    const d = await api(`/api/cases/${encodeURIComponent(c.case_id)}`);
    renderCaseDetail(d);
  } catch (e) {
    $('#cm-body').innerHTML = `<div class="cm-empty">读取失败：${esc(e.message)}</div>`;
  }
}

function closeCaseDetail() {
  $('#case-modal').classList.remove('on');
  $('#case-mask').classList.remove('on');
}

function renderCaseDetail(d) {
  const info = d.case || {};
  const runs = d.runs || [];
  cmCase = { ...(cmCase || {}), ...info };
  $('#cm-title').textContent = info.title || $('#cm-title').textContent;
  $('#cm-sub').textContent =
    `${info.platform || ''} · ${info.steps || 0} 步 · 命中 ${info.hit_count || 0} 次 · ` +
    `跑过 ${info.run_count || runs.length} 次`;

  const meta = [
    `<span class="cm-chip">${esc(info.platform || '')}</span>`,
    `<span class="cm-chip">${info.steps || 0} 步</span>`,
    info.images ? `<span class="cm-chip">${info.images} 图像步</span>` : '',
    info.screen && info.screen !== '0x0' ? `<span class="cm-chip">${esc(info.screen)}</span>` : '',
    info.last_status
      ? `<span class="cm-chip ${STATUS_CLS[info.last_status] || ''}">最近 ${STATUS_LABEL[info.last_status] || esc(info.last_status)}</span>`
      : '',
    info.source ? `<span class="cm-chip">来源 ${esc(info.source)}</span>` : '',
  ].filter(Boolean).join('');

  const runRows = runs.length
    ? runs.map((r, i) => `
        <button class="cm-run" type="button" data-run="${esc(r.run_id)}" data-i="${i}">
          <span class="cm-dot ${STATUS_CLS[r.status] || ''}"></span>
          <span class="cm-run-status">${STATUS_LABEL[r.status] || esc(r.status)}</span>
          <span class="cm-run-when">${fmtTime(r.started_at)}</span>
          <span class="cm-run-meta">${r.total_steps || 0} 步 · AI ${r.ai_steps || 0} · ${fmtMs(r.duration_ms)}</span>
          <span class="cm-run-mode">${esc(r.mode || '')}</span>
        </button>`).join('')
    : '<div class="cm-empty">这条用例还没有执行记录。</div>';

  // 标题就是用户那句原话时不再重复一遍（多数用例的 nl 与 title 相同）
  const nlBlock = info.nl && info.nl.trim() !== (info.title || '').trim()
    ? `<div class="cm-nl">${esc(info.nl)}</div>` : '';

  $('#cm-body').innerHTML = `
    ${nlBlock}
    <div class="cm-chips">${meta}</div>
    <section class="cm-section">
      <div class="cm-h">执行过程 <span>${runs.length} 次</span></div>
      <div class="cm-runs">${runRows}</div>
      <div class="cm-run-actions" id="cm-run-actions"></div>
    </section>
    <section class="cm-section">
      <div class="cm-h" id="cm-steps-title">步骤明细</div>
      <div class="cm-steps" id="cm-steps"></div>
    </section>`;

  const first = runs[0];
  if (first) {
    loadHistoryRun(first.run_id);
  } else {
    $('#cm-steps-title').textContent = `用例步骤（${(d.steps || []).length}）`;
    renderPlannedSteps(d.steps || []);
  }

  $$('#cm-body .cm-run').forEach((btn) => {
    btn.onclick = () => {
      $$('#cm-body .cm-run').forEach((x) => x.classList.toggle('on', x === btn));
      loadHistoryRun(btn.dataset.run);
    };
  });
}

function renderPlannedSteps(steps) {
  const box = $('#cm-steps');
  if (!steps.length) {
    box.innerHTML = '<div class="cm-empty">没有步骤信息。</div>';
    return;
  }
  box.innerHTML = steps.map((s) => `
    <div class="cm-step">
      <span class="cm-idx">${String(s.idx || 0).padStart(2, '0')}</span>
      <span class="act ${esc(s.action || '')}">${esc(s.action || '')}</span>
      <span class="cm-intent">${esc(s.intent || '')}</span>
      ${s.expect_kind && s.expect_kind !== 'none' ? `<span class="cm-expect">断言：${esc(s.expect_claim || s.expect_kind)}</span>` : ''}
    </div>`).join('');
}

async function loadHistoryRun(runId) {
  const box = $('#cm-steps');
  box.innerHTML = '<div class="cm-loading">读取执行明细…</div>';
  $('#cm-steps-title').textContent = '步骤明细';
  try {
    const d = await api(`/api/history/${encodeURIComponent(runId)}`);
    renderHistory(d);
  } catch (e) {
    box.innerHTML = `<div class="cm-empty">读取失败：${esc(e.message)}</div>`;
  }
}

function renderHistory(d) {
  const run = d.run || {};
  const steps = d.steps || [];
  $('#cm-steps-title').textContent =
    `步骤明细 · ${STATUS_LABEL[run.status] || run.status || ''} · ${fmtTime(run.started_at)}`;

  const actions = $('#cm-run-actions');
  if (actions) {
    actions.innerHTML = run.has_report
      ? `<a class="chip" href="/api/runs/${esc(run.run_id)}/report" target="_blank" rel="noopener">查看这次的报告</a>`
      : '';
  }

  const box = $('#cm-steps');
  if (!steps.length && !run.video) {
    box.innerHTML = '<div class="cm-empty">这次执行没有留下步骤明细。</div>';
    return;
  }
  const video = run.video
    ? `<div class="cm-video-wrap"><div class="cm-video-title">执行录屏</div>` +
      `<video controls preload="metadata" src="${esc(run.video)}"></video></div>`
    : '';
  const images = steps.filter((s) => s.image).map((s) => ({ url: s.image, label: s.intent || s.step_id }));
  box.innerHTML = video + steps.map((s, i) => `
    <div class="cm-step ${STATUS_CLS[s.status] || ''}">
      <span class="cm-idx">${String(s.idx || i + 1).padStart(2, '0')}</span>
      <span class="act ${esc(s.action || '')}">${esc(s.action || '')}</span>
      <span class="cm-intent">${esc(s.intent || '')}${s.error ? `<em>${esc(s.error)}</em>` : ''}</span>
      <span class="cm-step-meta">${s.judge_method ? esc(s.judge_method) + ' · ' : ''}${fmtMs(s.latency_ms)}</span>
      ${s.image ? `<img class="cm-thumb" src="${s.image}" loading="lazy" data-i="${images.findIndex((x) => x.url === s.image)}" alt="截图">` : ''}
    </div>`).join('');

  $$('#cm-body .cm-thumb').forEach((img) => {
    img.onclick = () => openLightbox(images, Number(img.dataset.i) || 0);
  });
}

function renderRuns(runs) {
  const box = $('#run-list');
  box.innerHTML = '';
  if (!runs.length) {
    box.innerHTML = '<div class="rail-empty">本次会话还没有执行记录。</div>';
    return;
  }
  for (const r of runs) {
    const item = document.createElement('div');
    item.className = 'case-item';
    const label = { passed: '通过', failed: '未通过', error: '异常', cancelled: '已取消', running: '执行中' }[r.status] || r.status;
    item.innerHTML = `
      <div class="ci-top">
        <span class="ci-title" title="${esc(r.title)}">${esc(r.title)}</span>
        <span class="ci-steps" style="color:${r.status === 'passed' ? '#34D399' : r.status === 'running' ? '#93A3BC' : '#F87171'}">${label}</span>
      </div>
      <div class="ci-sub"><b>${esc(r.platform)}</b><span>${fmtMs(r.duration_ms)}</span></div>
      <span class="ci-go">打开报告</span>`;
    item.onclick = () => {
      if (r.has_report) window.open(`/api/runs/${r.run_id}/report`, '_blank');
    };
    box.appendChild(item);
  }
}

/* ------------------------------------------------------------------ 发送 */
function bindEvents() {
  $('#send').onclick = () => send();
  $('#nl').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  $('#nl').addEventListener('input', autoGrow);

  $$('#mode button').forEach((b) => {
    b.onclick = () => {
      state.mode = b.dataset.mode;
      $$('#mode button').forEach((x) => x.classList.toggle('on', x === b));
    };
  });

  // 右侧设备面板：连接/断开、刷新、左右拖动调宽、画面直接操作
  initCastControls();
  initPanelResize();
  $('#cast-toggle').onclick = () => (state.cast.on ? stopCast() : startCast(true));
  $('#cast-refresh').onclick = refreshDevices;

  $('#exec-device').onclick = (e) => {
    e.stopPropagation();
    if ($('#exec-device-menu')) closeExecDeviceMenu();
    else openExecDeviceMenu(e.currentTarget);
  };
  // 左栏 #device / #refresh-devices 已随「执行终端」区块移除；
  // 刷新设备入口在 #exec-device 菜单和右侧投屏面板（#cast-refresh）。

  $('#open-settings').onclick = openDrawer;
  $('#close-drawer').onclick = closeDrawer;
  $('#drawer-mask').onclick = closeDrawer;
  $('#presets').onclick = (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $('#f-base').value = b.dataset.base;
    if (b.dataset.model) $('#f-model').value = b.dataset.model;
    // 本地预设：提示可以不填 Key，并把超时放宽（首次加载模型往往很慢）
    const local = b.dataset.local === '1';
    $('#f-key').placeholder = local ? '本地模型无需 Key，可留空' : 'sk-...（本地模型可留空）';
    if (local) $('#f-timeout').value = Math.max(parseInt($('#f-timeout').value || '60', 10), 120);
    $('#model-options').innerHTML = '';
    setTestBox('busy', local
      ? '已填入本地地址，可点「拉取模型列表」选择部署名'
      : '已填入服务地址，请补全 API Key');
  };
  $('#list-models').onclick = loadModelList;
  $('#test-model').onclick = () => submitModel(true);
  $('#save-model').onclick = () => submitModel(false);

  $$('#rail-tabs button').forEach((b) => {
    b.onclick = () => {
      $$('#rail-tabs button').forEach((x) => x.classList.toggle('on', x === b));
      $('#case-list').classList.toggle('hidden', b.dataset.tab !== 'cases');
      $('#run-list').classList.toggle('hidden', b.dataset.tab !== 'runs');
    };
  });

  $('#lb-close').onclick = closeLightbox;
  $('#lightbox').onclick = (e) => { if (e.target.id === 'lightbox') closeLightbox(); };
  $('#lb-prev').onclick = () => moveLightbox(-1);
  $('#lb-next').onclick = () => moveLightbox(1);

  $('#cm-close').onclick = closeCaseDetail;
  $('#cm-close2').onclick = closeCaseDetail;
  $('#case-mask').onclick = closeCaseDetail;
  $('#cm-replay').onclick = () => { if (cmCase) replayCase(cmCase); };
  $('#cm-delete').onclick = () => { if (cmCase) deleteCase(cmCase); };

  // 点空白处收起「···」菜单：菜单挂在 body 上，不能靠父节点的冒泡
  document.addEventListener('click', (e) => {
    if (ctxEl && !ctxEl.contains(e.target)) closeCaseMenu();
  });
  window.addEventListener('resize', closeCaseMenu);
  document.addEventListener('scroll', closeCaseMenu, true);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeLightbox(); closeDrawer(); closeCaseMenu(); closeCaseDetail(); }
    if ($('#lightbox').classList.contains('on')) {
      if (e.key === 'ArrowLeft') moveLightbox(-1);
      if (e.key === 'ArrowRight') moveLightbox(1);
    }
  });
}

function renderPresets() {
  const box = $('#presets');
  box.innerHTML = '';
  for (const p of PRESETS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = p.name;
    b.dataset.base = p.base;
    if (p.model) b.dataset.model = p.model;
    if (p.local) b.dataset.local = '1';
    box.appendChild(b);
  }
}

function renderExamples() {  const box = $('#examples');
  EXAMPLES.forEach((t, i) => {
    const b = document.createElement('button');
    b.className = 'example';
    b.type = 'button';
    b.innerHTML = `<span>0${i + 1}</span>${esc(t)}`;
    b.onclick = () => {
      $('#nl').value = t;
      autoGrow();
      $('#nl').focus();
    };
    box.appendChild(b);
  });
}

function autoGrow() {
  const el = $('#nl');
  el.style.height = 'auto';
  el.style.height = `${Math.min(180, el.scrollHeight)}px`;
}

async function send(override) {
  if (state.running) return;
  const text = String(override?.text ?? $('#nl').value ?? '').trim();
  if (!text) { $('#nl').focus(); return; }

  const payload = {
    text,
    // 标题就是原句，不截断：早先按 22 字硬切，卡片上只剩半句话
    //（「打开【示例聊天应用】，消息首页点击右上角的【+】，在」），用户根本认不出自己发了什么。
    // 换行、多余空格压成单个空格，剩下的交给 CSS 折行 + 悬停/点击展开。
    title: override?.title || text.replace(/\s+/g, ' ').trim(),
    case_id: override?.case_id || '',
    mode: override?.mode || state.mode,
    platform: state.platform,
    device: $('#device')?.value || '',   // 左栏下拉已移除，元素可能不存在
    heal: $('#heal').checked,
  };

  hideWelcome();
  appendUser(text, payload);
  $('#nl').value = '';
  autoGrow();

  state.running = true;
  $('#send').disabled = true;
  setPill('busy', '执行中');
  // 移动端执行时自动开投屏（手动开过的不打断也不重复连）
  if (MOBILE.includes(state.platform) && $('#cast-auto').checked && !state.cast.on) {
    startCast(false);
  }

  const card = createRunCard(payload);
  try {
    const { run_id } = await api('/api/run', payload);
    card.runId = run_id;
    poll(run_id, card);
  } catch (e) {
    card.fail(e.message);
    endRun();
  }
}

/* ------------------------------------------------------------------ 执行卡片 */
function createRunCard(payload) {
  const wrap = document.createElement('div');
  wrap.className = 'msg bot';
  wrap.innerHTML = `
    <div class="run-card" data-status="running">
      <div class="run-head">
        <span class="run-badge running">执行中</span>
        <span class="run-title" title="${esc(payload.title)}">${esc(payload.title)}</span>
        <span class="run-meta">${esc(payload.platform)} · ${esc(MODE_LABEL[payload.mode] || payload.mode)}</span>
        <button class="chip" data-cancel>取消</button>
      </div>
      <div class="run-progress"><i></i></div>
      <div class="run-logs"></div>
      <div class="steps"></div>
    </div>`;
  $('#stream').appendChild(wrap);
  scrollBottom();

  const card = {
    root: wrap.querySelector('.run-card'),
    head: wrap.querySelector('.run-head'),
    title: wrap.querySelector('.run-title'),
    badge: wrap.querySelector('.run-badge'),
    meta: wrap.querySelector('.run-meta'),
    progress: wrap.querySelector('.run-progress i'),
    logs: wrap.querySelector('.run-logs'),
    steps: wrap.querySelector('.steps'),
    payload,
    runId: '',
    caseId: payload.case_id || '',
    rows: new Map(),
    done: 0,
    total: 0,
    images: [],
  };
  // 长指令折到 3 行为止；真的被截了才让标题可点开，短标题点了也不会有反应
  requestAnimationFrame(() => {
    if (card.title.scrollHeight - card.title.clientHeight > 2) {
      card.title.classList.add('clamped');
    }
  });
  card.title.onclick = () => card.title.classList.toggle('expanded');
  wrap.querySelector('[data-cancel]').onclick = async (e) => {
    const btn = e.currentTarget;
    if (!card.runId || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = '中断中…';
    try {
      const r = await api(`/api/run/${card.runId}/cancel`, {});
      if (r.cancelling === false) {
        // 任务其实已经结束（只是前端还没收到终态事件）
        btn.textContent = '已结束';
        card.addLog(r.message || '任务已经结束', 'warn');
      } else {
        card.addLog('已请求中断：当前动作一返回就停，不会再往下执行', 'warn');
      }
    } catch (err) {
      btn.disabled = false;
      btn.textContent = '取消';
      card.addLog(`取消失败：${err.message}`, 'err');
    }
  };
  return {
    ...card,
    setBadge(text, cls) {
      card.badge.textContent = text;
      card.badge.className = `run-badge ${cls}`;
    },
    addLog(message, level = '') {
      const el = document.createElement('div');
      el.className = `run-log ${level}`;
      el.textContent = message;
      card.logs.appendChild(el);
      scrollBottom();
    },
    setProgress(done, total) {
      card.done = done; card.total = total;
      const bar = card.progress.parentElement;
      if (!total) {
        bar.classList.add('indet');
        card.progress.style.width = '';
        return;
      }
      bar.classList.remove('indet');
      card.progress.style.width = `${Math.round((done / total) * 100)}%`;
    },
    fail(message) {
      card.root.dataset.status = 'error';
      this.setBadge('异常', 'error');
      this.addLog(message, 'err');
    },
    images: [],
  };
}

function handleEvent(ev, card) {
  switch (ev.type) {
    case 'run_started':
      card.caseId = ev.case_id || card.caseId;
      card.meta.textContent = `${ev.platform}${ev.device ? ' · ' + ev.device : ''} · ${MODE_LABEL[ev.mode] || ev.mode}`;
      break;

    case 'log':
      card.addLog(ev.message, ev.level === 'warn' ? 'warn' : ev.level === 'error' ? 'err' : '');
      break;

    case 'thinking':
      card.setBadge('AI 规划中', 'running');
      break;

    case 'plan_done':
      card.setBadge('收尾中', 'running');
      break;

    case 'library_hit': {
      card.caseId = ev.case_id;
      const chip = document.createElement('span');
      chip.className = 'lib-hit';
      chip.textContent = `命中用例库 ${Math.round((ev.score || 0) * 100)}%`;
      chip.title = `${ev.title}（${ev.steps} 步）`;
      card.head.insertBefore(chip, card.head.querySelector('.run-meta'));
      card.addLog(`用例库命中「${ev.title}」，直接回放 ${ev.steps} 步脚本，不调用 AI 规划`);
      break;
    }

    case 'step_started':
      addStepRow(card, ev);
      break;

    case 'step_finished':
      updateStepRow(card, ev);
      break;

    case 'heal': {
      const row = card.rows.get(ev.index);
      if (row) {
        const note = document.createElement('span');
        note.className = 'step-note';
        note.textContent = 'AI 已修复定位器';
        note.title = (ev.strategies || []).join('\n');
        row.querySelector('.thumbs').before(note);
      }
      break;
    }

    case 'sedimented':
      if (ev.case_id) card.caseId = ev.case_id;
      card.addLog(`已沉淀 ${ev.steps} 步 → ${ev.stepir} / ${ev.script}`);
      break;

    case 'not_sedimented':
      card.addLog(ev.message || '本次执行未通过，不写入用例库', 'warn');
      break;

    case 'db_saved':
      card.addLog(ev.saved
        ? `已落库 SQLite（${ev.message}）`
        : `落库失败，仅保留文件产物：${ev.case_id}`, ev.saved ? '' : 'warn');
      break;

    case 'screen': {
      // 画布尺寸 + 相对基准的缩放比：换分辨率回放时这一行是排障的第一现场
      const sx = ev.scale_x || 1;
      const sy = ev.scale_y || 1;
      const scale = (Math.abs(sx - 1) > 0.001 || Math.abs(sy - 1) > 0.001)
        ? ` · 相对基准 ${sx.toFixed(2)}x×${sy.toFixed(2)}x`
        : '（与沉淀时一致）';
      card.addLog(`画布 ${ev.width}×${ev.height} · dpr ${ev.dpr ?? 1}${scale}`);
      break;
    }

    case 'app_resolved': {
      // 应用台账：第一次是"设备上找到并写回"，之后都是"台账命中"
      const how = ev.source === 'config'
        ? '台账命中'
        : '设备上找到，已写入 apps.json';
      card.addLog(`应用台账：${ev.name} → ${ev.app_id}（${how}）`);
      break;
    }

    case 'error':
      card.lastError = ev.message;
      card.fail(ev.message);
      break;

    case 'run_finished':
      if (ev.case_id) card.caseId = ev.case_id;
      renderVerdict(card, ev);
      break;

    default:
      break;
  }
}

function addStepRow(card, ev) {
  const action = ev.action || ev.result?.action || '';
  const intent = ev.intent || ev.result?.intent || '';
  const row = document.createElement('div');
  row.className = 'step';
  row.dataset.status = 'running';
  row.dataset.index = ev.index;
  row.innerHTML = `
    <span class="dot"></span>
    <span class="idx">${String(ev.index || 0).padStart(2, '0')}</span>
    <span class="act ${esc(action)}">${esc(action)}</span>
    <span class="intent">${esc(intent)}</span>
    <span class="meta">执行中…</span>
    <span class="thumbs"></span>`;
  card.steps.appendChild(row);
  card.rows.set(ev.index, row);
  card.setBadge('执行中', 'running');
  card.setProgress(card.done, card.total);
  scrollBottom();
}

function updateStepRow(card, ev) {
  const row = card.rows.get(ev.index);
  const r = ev.result || {};
  if (!row) { addStepRow(card, ev); return updateStepRow(card, ev); }

  row.dataset.status = r.status || 'passed';
  if (r.node_label) row.querySelector('.intent').title = r.node_label;

  const parts = [];
  if (r.strategy_used) parts.push(`定位 ${String(r.strategy_used).split(':')[0]}`);
  if (r.match_score) parts.push(`图像 ${Math.round(r.match_score * 100)}%`);
  if (r.judge_method && r.judge_method !== 'none') parts.push(`判定 ${r.judge_method}`);
  if (r.latency_ms) parts.push(fmtMs(r.latency_ms));
  const meta = row.querySelector('.meta');
  meta.textContent = parts.join(' · ') || (r.status === 'passed' ? '完成' : r.status || '');
  if (r.strategy_used) meta.title = r.strategy_used;

  if (r.error) {
    const err = document.createElement('div');
    err.className = 'err-line';
    err.textContent = r.error;
    row.appendChild(err);
  }

  const thumbs = row.querySelector('.thumbs');
  const imgs = ev.images || [];
  if (imgs.length) {
    card.images.push(...imgs.filter((i) => !card.images.some((x) => x.url === i.url)));
    imgs.forEach((img, i) => {
      const el = document.createElement('img');
      el.src = img.url;
      el.loading = 'lazy';
      el.title = img.label;
      // 证据文件不存在（门禁失败等早期返回的步骤）时别渲染成破损黑条
      el.onerror = () => {
        const ph = document.createElement('span');
        ph.className = 'thumb-missing';
        ph.title = img.label;
        ph.textContent = '无截图';
        el.replaceWith(ph);
      };
      el.onclick = () => openLightbox(imgs, i);
      thumbs.appendChild(el);
    });
  }

  card.setProgress((card.done || 0) + 1, card.total || 0);
  scrollBottom();
}

function renderVerdict(card, ev) {
  const st = ev.status || 'failed';
  const s = ev.summary || {};
  const label = { passed: '通过', failed: '未通过', error: '执行异常', cancelled: '已取消' }[st] || st;

  card.root.dataset.status = st;
  card.setBadge(label, st);
  card.setProgress(card.done || 1, card.total || card.done || 1);

  const cancelBtn = card.head.querySelector('[data-cancel]');
  if (cancelBtn) cancelBtn.remove();
  if (st === 'cancelled' && card.payload) {
    // 取消之后仍允许一键重跑
    const again = document.createElement('button');
    again.className = 'chip';
    again.textContent = '重新执行';
    again.onclick = () => send({ text: card.payload.text, title: card.payload.title, mode: card.payload.mode });
    card.head.appendChild(again);
  }

  const stats = [];
  if (s.total_steps) {
    stats.push(`<span>步骤 <b>${s.total_steps - (s.failed_steps || 0)}/${s.total_steps}</b></span>`);
    const ratio = s.ai_ratio != null ? Math.round(s.ai_ratio * 100) : 0;
    stats.push(`<span>AI 参与 <b>${s.ai_steps}</b> 步 (${ratio}%)</span>`);
  }
  if (s.duration_ms) stats.push(`<span>耗时 <b>${(s.duration_ms / 1000).toFixed(1)}s</b></span>`);
  if (ev.platform) {
    const dev = ev.device && ev.device !== ev.platform ? ` · ${ev.device}` : '';
    stats.push(`<span>${esc(ev.platform)}${esc(dev)}</span>`);
  }
  if (ev.screen_label) stats.push(`<span title="本次画布尺寸 / 相对沉淀基准的缩放">${esc(ev.screen_label)}</span>`);

  const actions = [];
  if (ev.report_url) actions.push(`<a class="chip" href="${ev.report_url}" target="_blank" rel="noopener">查看报告</a>`);
  if (ev.script_url) actions.push(`<a class="chip" href="${ev.script_url}">下载脚本</a>`);
  if (ev.stepir_url) actions.push(`<a class="chip" href="${ev.stepir_url}">StepIR</a>`);
  if (card.payload) actions.push('<button class="chip" data-replay>再次回放</button>');

  const box = document.createElement('div');
  box.className = `verdict ${st}`;
  box.innerHTML = `
    <div class="verdict-top">
      <span class="verdict-status">${label}</span>
      <div class="verdict-stats">${stats.join('')}</div>
    </div>
    <div class="verdict-actions">${actions.join('')}</div>`;
  card.root.appendChild(box);

  const replay = box.querySelector('[data-replay]');
  if (replay) {
    replay.onclick = () => send({
      text: card.payload.text,
      title: card.payload.title,
      case_id: card.caseId,
      mode: 'replay',
    });
  }
  if (st === 'error' && /模型未配置|API Key|Base URL/.test(ev.message || '')) {
    const b = document.createElement('button');
    b.className = 'chip';
    b.textContent = '去配置模型';
    b.onclick = openDrawer;
    box.querySelector('.verdict-actions').appendChild(b);
  }
  if ((st === 'error' || st === 'cancelled') && ev.message && ev.message !== card.lastError) {
    card.addLog(ev.message, 'err');
  }
  scrollBottom();
}

function endRun() {
  state.running = false;
  $('#send').disabled = false;
  setPill('ok', '就绪');
  // 跟随执行自动开的投屏，用完就收；用户手动开的保持，方便继续观察设备
  if (state.cast.on && !state.cast.manual) stopCast();
  refreshSide();
}

async function refreshSide() {
  try {
    const [c, r] = await Promise.all([api('/api/cases'), api('/api/runs')]);
    state.cases = c.cases || [];
    renderCases();
    renderRuns(r.runs || []);
  } catch (e) {
    console.error(e);
  }
}

async function poll(runId, card) {
  let since = 0;
  let finished = false;
  let guard = 0;
  let lastStatus = 'running';
  while (!finished && guard < 4000) {
    guard += 1;
    let data;
    try {
      data = await api(`/api/runs/${runId}/events?since=${since}&wait=20`);
    } catch (e) {
      card.fail(`事件流中断：${e.message}`);
      break;
    }
    lastStatus = data.status || lastStatus;
    for (const ev of data.events || []) {
      handleEvent(ev, card);
      if (ev.type === 'run_finished') finished = true;
    }
    since = data.next ?? since;
    if (!finished && TERMINAL.includes(lastStatus) && !(data.events || []).length) {
      finished = true;
    }
  }
  // 兜底：事件流异常中断时，至少把结论渲染出来，不能让卡片永远停在「执行中」
  if (!card.root.querySelector('.verdict')) {
    renderVerdict(card, {
      status: TERMINAL.includes(lastStatus) ? lastStatus : 'error',
      summary: null,
      message: '未收到完整的结束事件，请查看服务端日志或重跑一次',
    });
  }
  endRun();
}

/* ------------------------------------------------------------------ 杂项 */
function hideWelcome() {
  const w = $('#welcome');
  if (w) w.remove();
}

function scrollBottom() {
  const s = $('#stream');
  const near = s.scrollHeight - s.scrollTop - s.clientHeight < 220;
  if (near) s.scrollTop = s.scrollHeight;
}

function setPill(kind, text) {
  const p = $('#conn-pill');
  p.className = `pill ${kind || ''}`;
  p.querySelector('span').textContent = text;
}

let toastTimer = 0;

function toast(message, kind) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.className = `toast ${kind || ''} on`;
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), 3600);
}

function appendUser(text, payload) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `
    <div class="bubble">
      ${esc(text)}
      <div class="bub-meta">
        <span>${esc(payload.platform)}</span>
        <span>${esc(MODE_LABEL[payload.mode] || payload.mode)}</span>
        <span>${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
      </div>
    </div>`;
  $('#stream').appendChild(el);
  scrollBottom();
}

/* ------------------------------------------------------------------ 模型抽屉 */
function openDrawer() {
  const m = state.model || {};
  $('#f-base').value = m.base_url || '';
  $('#f-key').value = '';
  $('#f-key').placeholder = m.api_key_masked ? `已保存 ${m.api_key_masked}，留空不改` : 'sk-...（本地模型可留空）';
  $('#f-model').value = m.model || '';
  $('#f-vision').value = m.vision_model || '';
  $('#f-temp').value = m.temperature ?? 0;
  $('#f-timeout').value = m.timeout_s ?? 60;
  $('#f-vision-on').checked = m.vision !== false;
  $('#f-throttle-on').checked = m.throttle !== false;
  $('#f-max-per-min').value = m.max_per_min ?? 20;
  $('#f-min-interval').value = m.min_interval_ms ?? 1200;
  $('#key-state').textContent = m.has_key ? '已配置' : (m.is_local ? '本地模型可留空' : '');
  $('#drawer').classList.add('on');
  $('#drawer-mask').classList.add('on');
  setTimeout(() => $('#f-base').focus(), 240);
}

function closeDrawer() {
  $('#drawer').classList.remove('on');
  $('#drawer-mask').classList.remove('on');
}

function modelForm() {
  return {
    base_url: $('#f-base').value.trim(),
    api_key: $('#f-key').value.trim(),
    model: $('#f-model').value.trim(),
    vision_model: $('#f-vision').value.trim(),
    temperature: parseFloat($('#f-temp').value || '0'),
    timeout_s: parseInt($('#f-timeout').value || '60', 10),
    vision: $('#f-vision-on').checked,
    throttle: $('#f-throttle-on').checked,
    max_per_min: parseInt($('#f-max-per-min').value || '20', 10),
    min_interval_ms: parseInt($('#f-min-interval').value || '1200', 10),
  };
}

function setTestBox(kind, text) {
  const box = $('#test-box');
  box.className = `test-box ${kind}`;
  $('#test-text').textContent = text;
}

async function loadModelList() {
  const form = modelForm();
  if (!form.base_url) return setTestBox('err', '请先填写 Base URL');
  setTestBox('busy', '正在向服务端拉取模型列表…');
  try {
    const r = await api('/api/model/list', form);
    if (!r.ok) return setTestBox('err', r.detail || '拉取失败');
    const dl = $('#model-options');
    dl.innerHTML = '';
    (r.models || []).forEach((id) => {
      const o = document.createElement('option');
      o.value = id;
      dl.appendChild(o);
    });
    if (!form.model && r.models && r.models.length) $('#f-model').value = r.models[0];
    setTestBox('ok', `已拉取 ${r.models.length} 个模型，点模型名输入框可直接选`);
  } catch (e) {
    setTestBox('err', e.message);
  }
}

async function submitModel(withTest) {
  const form = modelForm();
  if (!form.base_url || !form.model) {
    return setTestBox('err', 'Base URL 与模型名必须填写');
  }
  // 本地/局域网服务不需要 Key（Ollama、LM Studio、vLLM 的默认行为）
  if (!form.api_key && !(state.model?.has_key) && !isLocalBase(form.base_url)) {
    return setTestBox('err', '公网模型需要填写 API Key（本地/局域网可留空）');
  }

  if (!withTest) {
    try {
      const r = await api('/api/model', form);
      state.model = r.model;
      renderModelBadge();
      setTestBox('ok', '配置已保存');
      setTimeout(closeDrawer, 500);
    } catch (e) {
      setTestBox('err', e.message);
    }
    return;
  }

  setTestBox('busy', '正在请求模型…');
  $('#f-key').value = '';
  try {
    const r = await api('/api/model/test', form);
    if (r.ok) {
      const pathLabel = r.path === 'direct' ? '直连（本地/绕过代理）'
        : r.path === 'auto' ? '系统/环境代理' : '';
      setTestBox('ok', `连接成功 · ${r.latency_ms}ms`
        + (pathLabel ? ` · ${pathLabel}` : '') + ` · 返回「${r.detail}」`);
      state.model = (await api('/api/state')).model;
      renderModelBadge();
    } else {
      setTestBox('err', r.detail || '连接失败');
    }
  } catch (e) {
    setTestBox('err', e.message);
  }
}

/* ------------------------------------------------------------------ 图片 */
let lbState = { images: [], index: 0 };

function openLightbox(images, index) {
  lbState = { images: images || [], index: index || 0 };
  paintLightbox();
  $('#lightbox').classList.add('on');
}

function paintLightbox() {
  const { images, index } = lbState;
  if (!images.length) return;
  const cur = images[index];
  $('#lb-img').src = cur.url;
  $('#lb-label').textContent = `${cur.label || '截图'} · ${index + 1}/${images.length}`;
  $('#lb-prev').disabled = index <= 0;
  $('#lb-next').disabled = index >= images.length - 1;
}

function moveLightbox(delta) {
  const next = lbState.index + delta;
  if (next < 0 || next >= lbState.images.length) return;
  lbState.index = next;
  paintLightbox();
}

function closeLightbox() {
  $('#lightbox').classList.remove('on');
}

boot();

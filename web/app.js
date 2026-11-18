/* DXF / DWG 互转工具 —— 前端逻辑（原生 JS，无外部依赖） */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const CAD_PATH_MIME = 'application/x-cad-paths';

const state = {
  cwd: '', dirs: [], files: [], roots: [], selected: new Set(),
  queue: [], active: -1, customDir: null, suffix: '',
  settings: {}, engines: {}, versions: { dwg: [], dxf: [], oda: [] },
  picker: { cwd: '', onConfirm: null },
  zoom: 1, panX: 0, panY: 0, running: false, lastOutputDir: '',
};

async function api(path, opts) {
  const r = await fetch(path, opts || {});
  let data;
  try { data = await r.json(); } catch (e) { throw new Error('服务返回异常'); }
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
const get = (p, q) => api(p + (q ? '?' + new URLSearchParams(q) : ''));
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

function dirname(p) {
  const i = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
  if (i < 0) return '';
  if (i === 2 && /^[A-Za-z]:/.test(p)) return p.slice(0, 3);
  if (i === 0) return p.slice(0, 1);
  return p.slice(0, i);
}
function joinPath(dir, name) {
  if (!dir) return name;
  const sep = dir.includes('/') && !dir.includes('\\') ? '/' : '\\';
  return /[\\/]$/.test(dir) ? dir + name : dir + sep + name;
}
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}
function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('toastWrap').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
function log(msg, cls) {
  const box = $('logBody');
  const line = document.createElement('div');
  line.className = cls ? 'l-' + cls : '';
  line.textContent = '[' + new Date().toLocaleTimeString('zh-CN', { hour12: false }) + '] ' + msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 文件浏览器 ---------------- */
function renderCrumb(path) {
  const box = $('crumb');
  if (!path) { box.innerHTML = '<span>请先选择磁盘或常用目录</span>'; return; }
  const parts = [];
  const m = path.match(/^([A-Za-z]:[\\/]|\/)/);
  if (m) { parts.push({ label: m[1], path: m[1] }); }
  const rest = m ? path.slice(m[1].length) : path;
  let acc = m ? m[1] : '';
  rest.split(/[\\/]/).filter(Boolean).forEach((seg) => {
    acc = /[\\/]$/.test(acc) ? acc + seg : acc + (acc ? '\\' : '') + seg;
    parts.push({ label: seg, path: acc });
  });
  box.innerHTML = parts.map((p, i) =>
    (i ? '<i> › </i>' : '') + '<span data-p="' + esc(p.path) + '">' + esc(p.label) + '</span>').join('');
  box.querySelectorAll('span[data-p]').forEach((s) =>
    s.onclick = () => loadList(s.dataset.p));
}

function renderRoots() {
  const chips = state.roots.map((r) =>
    '<button class="chip" data-p="' + esc(r.path) + '">' + esc(r.name) + '</button>').join('');
  $('rootChips').innerHTML = chips + '<button class="chip" id="chipSelectAll">全选本目录 CAD 文件</button>';
  $('rootChips').querySelectorAll('.chip[data-p]').forEach((c) =>
    c.onclick = () => loadList(c.dataset.p));
  $('chipSelectAll').onclick = () => {
    state.files.forEach((f) => state.selected.add(f.path));
    renderFiles();
    toast('已勾选本目录 ' + state.files.length + ' 个 CAD 文件', 'ok');
  };
}

function renderFiles() {
  const box = $('fileList');
  if (state.cwd === '') {
    box.innerHTML = '<div class="empty-note">从上方选择磁盘或常用目录开始浏览</div>';
    return;
  }
  const rows = [];
  state.dirs.forEach((d) => rows.push(
    '<div class="file-row" data-dir="' + esc(d.path) + '">' +
    '<span class="fcheck"></span><span class="fi">📁</span>' +
    '<span class="fname">' + esc(d.name) + '</span><span class="fmeta">文件夹</span></div>'));
  state.files.forEach((f) => {
    const on = state.selected.has(f.path);
    const tag = f.ext === '.dwg' ? 'ext-dwg' : 'ext-dxf';
    rows.push(
      '<div class="file-row' + (on ? ' selected' : '') + '" data-file="' + esc(f.path) + '" draggable="true" ' +
      'title="可拖到中间转换队列">' +
      '<span class="fcheck">' + (on ? '✓' : '') + '</span>' +
      '<span class="fi">📄</span><span class="fname">' + esc(f.name) + '</span>' +
      '<span class="ext-tag ' + tag + '">' + esc(f.ext.slice(1).toUpperCase()) + '</span>' +
      '<span class="fmeta">' + fmtSize(f.size) + '</span></div>');
  });
  if (!rows.length) rows.push('<div class="empty-note">此文件夹没有 DXF / DWG 文件</div>');
  box.innerHTML = rows.join('');
  box.querySelectorAll('[data-dir]').forEach((r) =>
    r.onclick = () => loadList(r.dataset.dir));
  box.querySelectorAll('[data-file]').forEach((r) => {
    r.onclick = () => {
      const p = r.dataset.file;
      state.selected.has(p) ? state.selected.delete(p) : state.selected.add(p);
      renderFiles();
    };
    r.addEventListener('dragstart', (e) => {
      const p = r.dataset.file;
      // 拖已勾选的行时，把整批勾选文件一起带走；否则只带这一行
      const paths = (state.selected.has(p) && state.selected.size > 1)
        ? [...state.selected] : [p];
      e.dataTransfer.setData(CAD_PATH_MIME, JSON.stringify(paths));
      e.dataTransfer.setData('text/plain', paths.join('\n'));
      e.dataTransfer.effectAllowed = 'copy';
      r.classList.add('dragging-row');
    });
    r.addEventListener('dragend', () => {
      r.classList.remove('dragging-row');
      $('queueList').classList.remove('drop-target');
      $('dropOverlay').classList.remove('show');
    });
  });
  $('selCount').textContent = state.selected.size;
}

async function loadList(path) {
  try {
    const d = await get('/api/list', { path: path || '', filter: 'cad' });
    if (d.error) { toast(d.error, 'err'); return; }
    state.cwd = d.path || '';
    state.dirs = d.dirs || [];
    state.files = d.files || [];
    $('pathInput').value = state.cwd;
    localStorage.setItem('cad_last_dir', state.cwd);
    renderCrumb(state.cwd);
    renderFiles();
  } catch (e) { toast('读取目录失败：' + e.message, 'err'); }
}

function makeItem(info) {
  const ext = info.ext;
  const outExt = ext === '.dwg' ? '.dxf' : '.dwg';
  const stem = info.name.slice(0, info.name.length - ext.length);
  return {
    input: info.path, name: info.name, ext, size: info.size, version: info.version,
    outputDir: state.customDir || dirname(info.path),
    outputName: stem + state.suffix + outExt,
    isCustom: !!state.customDir, nameEdited: false,
    status: 'idle', message: '', result: '', svg: null,
  };
}

async function addPaths(paths, opts) {
  const o = opts || {};
  let added = 0;
  const addedIdx = [];
  for (const p of paths) {
    if (!/\.(dxf|dwg)$/i.test(p)) continue;
    if (state.queue.some((q) => q.input.toLowerCase() === p.toLowerCase())) continue;
    try {
      const info = await get('/api/info', { path: p });
      if (info.error) continue;
      const item = makeItem(info);
      if (o.dropped) {
        item.dropped = true;
        item.outputDir = o.outputDir || item.outputDir;
        item.isCustom = true;
      }
      addedIdx.push(state.queue.length);
      state.queue.push(item);
      added++;
    } catch (e) { /* 忽略单个无效路径 */ }
  }
  renderQueue();
  if (added) {
    toast('已加入 ' + added + ' 个文件到转换队列', 'ok');
    if (state.active < 0) setActive(state.queue.length - 1);
    state.selected.clear();
    renderFiles();
  } else if (!o.silent) {
    toast('没有新增文件（可能已在队列中或路径无效）', 'warn');
  }
  return addedIdx;
}

/* ---------------- 拖放支持 ----------------
 * 两种拖拽：
 *  A. 左侧列表项 → 中间队列：走自定义 MIME 传真实磁盘路径，仍保持「默认同文件夹」。
 *  B. 系统资源管理器 → 页面：浏览器拿不到真实路径，只能把文件本体上传到本机
 *     uploads/，加入队列后提示手动指定输出地址（未指定时兜底到「转换输出」目录）。
 *     少数文件管理器会在 text/plain 里带完整路径，此时优先按真实路径处理。
 * ---------------------------------------- */
let dragDepth = 0;
let dropBusy = false;

function dtTypes(e) {
  return Array.from((e.dataTransfer && e.dataTransfer.types) || []);
}
function isInternalDrag(e) { return dtTypes(e).includes(CAD_PATH_MIME); }
function isFileDrag(e) {
  const t = dtTypes(e);
  return t.includes('Files') && !t.includes(CAD_PATH_MIME);
}
function hideDropUI() {
  dragDepth = 0;
  $('dropOverlay').classList.remove('show');
  $('queueList').classList.remove('drop-target');
  $('fileList').classList.remove('drop-target');
}
function closestEl(e, sel) {
  return e.target && e.target.closest ? e.target.closest(sel) : null;
}

async function tryRealPaths(text, count) {
  if (!text || !count) return [];
  const cands = text.split(/\r?\n/)
    .map((s) => s.trim().replace(/^"|"$/g, ''))
    .filter((s) => /^(?:[A-Za-z]:[\\/]|\\\\)/.test(s) && /\.(dxf|dwg)$/i.test(s));
  const out = [];
  for (const p of cands) {
    try {
      const d = await get('/api/info', { path: p });
      if (!d.error && d.path) out.push(d.path);
    } catch (err) { /* 无效路径忽略 */ }
  }
  // 只有全部文件都拿到真实路径才采用，避免漏加文件
  return out.length === count ? out : [];
}

async function uploadOne(file) {
  const r = await fetch('/api/upload', {
    method: 'POST',
    headers: {
      'X-Cad-Name': encodeURIComponent(file.name),
      'Content-Type': 'application/octet-stream',
    },
    body: file,
  });
  let d;
  try { d = await r.json(); } catch (err) { throw new Error('服务返回异常'); }
  if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

async function handleSystemDrop(dt) {
  if (dropBusy) return;
  const all = Array.from(dt.files || []);
  const files = all.filter((f) => /\.(dxf|dwg)$/i.test(f.name));
  if (!files.length) {
    let hasDir = false;
    try {
      hasDir = Array.from(dt.items || []).some((it) => {
        const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
        return en && en.isDirectory;
      });
    } catch (err) { /* 忽略 */ }
    toast(hasDir
      ? '暂不支持直接拖入文件夹，请用左侧目录浏览选择'
      : '拖入的内容中没有 DXF / DWG 文件', 'warn');
    return;
  }
  const ignored = all.length - files.length;

  // 优先尝试拖拽文本里的真实磁盘路径（可保留默认同文件夹规则）
  const real = await tryRealPaths(dt.getData('text/plain') || '', files.length);
  if (real.length) {
    await addPaths(real);
    toast('已识别原始磁盘路径，按默认规则输出到源文件所在文件夹', 'ok');
    return;
  }

  dropBusy = true;
  const paths = [];
  let defaultOut = '';
  let failed = 0;
  for (const f of files) {
    try {
      const d = await uploadOne(f);
      paths.push(d.path);
      if (d.defaultOut) defaultOut = d.defaultOut;
    } catch (err) {
      failed++;
      log('拖入文件接收失败：' + f.name + ' · ' + err.message, 'err');
      toast('「' + f.name + '」接收失败：' + err.message, 'err');
    }
  }
  dropBusy = false;
  if (!paths.length) return;

  const useCustom = !!state.customDir;
  const idxs = await addPaths(paths, {
    dropped: !useCustom,
    outputDir: state.customDir || defaultOut,
  });
  if (!idxs.length) return;
  if (ignored || failed) {
    toast('忽略 ' + ignored + ' 个非 CAD 文件' + (failed ? '，接收失败 ' + failed + ' 个' : ''), 'warn');
  }
  if (!useCustom) {
    log('拖入 ' + idxs.length + ' 个文件，已暂存本机，等待指定输出地址');
    toast('已加入 ' + idxs.length + ' 个拖入文件；请选择输出地址（未选则输出到工具目录「转换输出」）', 'warn');
    openPicker({
      title: '拖入的文件请指定输出地址',
      sub: '浏览器无法读取拖拽文件的原始磁盘路径，文件已暂存到本机',
      cwd: state.cwd || '',
      onConfirm: (dir) => {
        idxs.forEach((i) => {
          const q = state.queue[i];
          if (q) { q.outputDir = dir; q.isCustom = true; }
        });
        renderQueue();
        toast('已为拖入的 ' + idxs.length + ' 个文件设置输出地址', 'ok');
      },
    });
  }
}

function bindDrop() {
  window.addEventListener('dragenter', (e) => {
    if (isInternalDrag(e)) {
      e.preventDefault();
      $('queueList').classList.add('drop-target');
      return;
    }
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth++;
    $('dropOverlay').classList.add('show');
  });

  window.addEventListener('dragover', (e) => {
    if (isInternalDrag(e)) {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
      $('queueList').classList.add('drop-target');
      return;
    }
    if (!isFileDrag(e)) return;
    e.preventDefault();                       // 必须，否则浏览器不触发 drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    $('dropOverlay').classList.add('show');
    $('queueList').classList.toggle('drop-target', !!closestEl(e, '.queue-panel'));
  });

  window.addEventListener('dragleave', (e) => {
    if (isInternalDrag(e) || !isFileDrag(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0 || !e.relatedTarget) hideDropUI();
  });

  window.addEventListener('drop', async (e) => {
    const internal = isInternalDrag(e);
    const external = isFileDrag(e);
    if (!internal && !external) return;
    e.preventDefault();                       // 阻止浏览器直接打开拖入的文件
    hideDropUI();
    if (internal) {
      let paths = [];
      try { paths = JSON.parse(e.dataTransfer.getData(CAD_PATH_MIME) || '[]'); }
      catch (err) { paths = []; }
      if (!paths.length) {
        paths = (e.dataTransfer.getData('text/plain') || '').split(/\r?\n/)
          .map((s) => s.trim()).filter((s) => /\.(dxf|dwg)$/i.test(s));
      }
      if (paths.length) await addPaths(paths);
      return;
    }
    await handleSystemDrop(e.dataTransfer);
  });
}

/* ---------------- 转换队列 ---------------- */
function statusLabel(s) {
  return { idle: '待转换', running: '转换中…', done: '已完成', failed: '失败', skipped: '已跳过' }[s] || s;
}
function renderQueue() {
  const box = $('queueList');
  $('btnConvert').disabled = state.running || state.queue.length === 0;
  const total = state.queue.length;
  const done = state.queue.filter((q) => q.status === 'done').length;
  $('queueSummary').innerHTML = total
    ? '共 <b>' + total + '</b> 个文件 · <b>' + state.queue.filter(q => q.ext === '.dwg').length +
      '</b> 个 DWG→DXF · <b>' + state.queue.filter(q => q.ext === '.dxf').length +
      '</b> 个 DXF→DWG · 已完成 <b>' + done + '</b>'
    : '<span>尚未添加文件</span>';
  if (!total) {
    box.innerHTML = '<div class="empty-state"><div class="empty-illust">' +
      '<svg viewBox="0 0 90 70" width="90" height="70">' +
      '<rect x="8" y="12" width="46" height="46" rx="5" fill="#eef3fb" stroke="#cbd8ec" stroke-width="2"/>' +
      '<path d="M18 45 L28 30 L36 40 L45 24" fill="none" stroke="#7ba0d8" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>' +
      '<rect x="40" y="20" width="42" height="46" rx="5" fill="#fff" stroke="#cbd8ec" stroke-width="2"/>' +
      '<path d="M50 50 L58 36 L66 45 L74 30" fill="none" stroke="#3b7ae0" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>' +
          '</svg></div><b>从左侧选择，或直接把 DXF / DWG 拖到这里</b>' +
          '<span>左侧列表项可拖入队列；系统文件拖入后需手动指定输出地址</span></div>';
    return;
  }
  box.innerHTML = state.queue.map((q, i) => {
    const srcTag = q.ext === '.dwg' ? 'ext-dwg' : 'ext-dxf';
    const outExt = q.ext === '.dwg' ? '.dxf' : '.dwg';
    const cls = 'qcard ' + q.status + (i === state.active ? ' active' : '');
    const dirText = q.isCustom ? q.outputDir : '同文件夹 · ' + q.outputDir;
    let result = '';
    if (q.result) {
      result = '<div class="qresult ' + (q.status === 'failed' ? 'err' : '') + '">' +
        '<span>' + esc(q.message) + '</span>' +
        (q.status === 'done' && q.result !== 'skipped'
          ? '<a data-reveal="' + esc(q.result) + '">打开所在文件夹</a><a data-dl="' + esc(q.result) + '">下载</a>' : '') +
        '</div>';
    }
    return '<div class="' + cls + '" data-idx="' + i + '">' +
      '<div class="qcard-top">' +
        '<div class="qthumb" data-thumb="' + i + '">' + (q.svg || '▤') + '</div>' +
        '<div class="qinfo"><div class="qname" title="' + esc(q.input) + '">' + esc(q.name) + '</div>' +
          '<div class="qsub"><span class="ext-tag ' + srcTag + '">' + esc(q.ext.slice(1).toUpperCase()) + '</span>' +
          (q.dropped ? '<span class="drop-tag">拖入文件</span>' : '') +
          '<span>' + fmtSize(q.size) + '</span><span>版本 ' + esc(q.version) + '</span>' +
          '<span>→ ' + outExt.slice(1).toUpperCase() + '</span></div></div>' +
        '<span class="qstatus ' + q.status + '">' + statusLabel(q.status) + '</span>' +
        '<button class="icon-btn qremove" data-remove="' + i + '" title="移除">✕</button>' +
      '</div>' +
      '<div class="qflow"><span class="qflow-label">输出地址</span>' +
        '<span class="qdir ' + (q.isCustom ? '' : 'same') + '" title="' + esc(q.outputDir) + '">' + esc(dirText) + '</span>' +
        '<button class="qchange" data-change="' + i + '">更改…</button></div>' +
      '<div class="qname-row"><span class="qflow-label">文件名</span>' +
        '<input class="qout-name" data-name="' + i + '" value="' + esc(q.outputName) + '" spellcheck="false"></div>' +
      result + '</div>';
  }).join('');

  box.querySelectorAll('.qcard').forEach((c) =>
    c.onclick = (e) => {
      if (e.target.closest('button, input, a')) return;
      setActive(Number(c.dataset.idx));
    });
  box.querySelectorAll('[data-remove]').forEach((b) => b.onclick = () => {
    const i = Number(b.dataset.remove);
    state.queue.splice(i, 1);
    if (state.active >= state.queue.length) state.active = state.queue.length - 1;
    renderQueue();
  });
  box.querySelectorAll('[data-change]').forEach((b) => b.onclick = () => {
    const i = Number(b.dataset.change);
    openPicker({
      title: '为「' + state.queue[i].name + '」选择输出地址',
      sub: '仅修改此文件的输出文件夹',
      cwd: state.queue[i].outputDir,
      onConfirm: (dir) => {
        state.queue[i].outputDir = dir;
        state.queue[i].isCustom = true;
        renderQueue();
        toast('已单独设置输出地址', 'ok');
      },
    });
  });
  box.querySelectorAll('[data-name]').forEach((inp) => inp.onchange = () => {
    const i = Number(inp.dataset.name);
    state.queue[i].outputName = inp.value.trim() || state.queue[i].outputName;
    state.queue[i].nameEdited = true;
  });
  box.querySelectorAll('[data-reveal]').forEach((a) => a.onclick = async () => {
    try { await post('/api/reveal', { path: a.dataset.reveal }); }
    catch (e) { toast(e.message, 'err'); }
  });
  box.querySelectorAll('[data-dl]').forEach((a) => a.onclick = () => {
    window.location.href = '/api/download?path=' + encodeURIComponent(a.dataset.dl);
  });
}

/* ---------------- 预览 ---------------- */
function applyTransform() {
  $('previewInner').style.transform =
    'translate(' + state.panX + 'px,' + state.panY + 'px) scale(' + state.zoom + ')';
}
function fitPreview() { state.zoom = 1; state.panX = 0; state.panY = 0; applyTransform(); }
async function setActive(i) {
  if (i < 0 || i >= state.queue.length) {
    $('previewInner').innerHTML = '<div class="preview-placeholder">选中队列中的文件即可预览图纸</div>';
    $('previewMeta').textContent = '—';
    return;
  }
  state.active = i;
  const q = state.queue[i];
  renderQueue();
  $('previewInner').innerHTML = '<div class="preview-placeholder">正在解析图纸…</div>';
  $('previewMeta').textContent = q.name;
  try {
    const r = await get('/api/preview', { path: q.input });
    if (state.active !== i) return;
    $('previewInner').innerHTML = r.svg || ('<div class="preview-placeholder">' + esc(r.error || '无预览') + '</div>');
    fitPreview();
    if (r.ok) {
      q.svg = r.svg;
      const thumb = document.querySelector('[data-thumb="' + i + '"]');
      if (thumb) thumb.innerHTML = r.svg;
      $('previewMeta').textContent = r.count + ' 个图元 · ' + (r.layers || []).length + ' 个图层' +
        (r.bounds && r.bounds.length ? ' · 范围 ' + r.bounds.join(', ') : '') +
        (r.truncated ? ' · 仅显示前 8000 个' : '');
    } else {
      $('previewMeta').textContent = r.error || '预览失败';
    }
  } catch (e) {
    $('previewInner').innerHTML = '<div class="preview-placeholder">预览失败</div>';
    $('previewMeta').textContent = e.message;
  }
}

/* ---------------- 目录选择弹窗 ---------------- */
async function loadPicker(path) {
  state.picker.cwd = path;
  $('pickerPathInput').value = path;
  $('pickerCurrent').textContent = '当前目录：' + (path || '—');
  try {
    const d = await get('/api/list', { path: path || '', filter: 'dirs' });
    if (d.error) { toast(d.error, 'err'); return; }
    state.picker.cwd = d.path || '';
    $('pickerCurrent').textContent = '当前目录：' + (d.path || '—');
    $('pickerPathInput').value = d.path || '';
    const rows = [];
    if (d.parent && d.parent !== d.path) rows.push(
      '<div class="picker-row" data-up="1"><span class="fi">↰</span>上级目录</div>');
    (d.dirs || []).forEach((x) => rows.push(
      '<div class="picker-row" data-dir="' + esc(x.path) + '"><span class="fi">📁</span>' + esc(x.name) + '</div>'));
    if (!rows.length) rows.push('<div class="empty-note">没有子文件夹</div>');
    $('pickerList').innerHTML = rows.join('');
    $('pickerList').querySelectorAll('[data-dir]').forEach((r) => r.onclick = () => loadPicker(r.dataset.dir));
    const up = $('pickerList').querySelector('[data-up]');
    if (up) up.onclick = () => loadPicker(d.parent || '');
  } catch (e) { toast(e.message, 'err'); }
}
function openPicker(opt) {
  $('pickerTitle').textContent = opt.title || '选择输出文件夹';
  $('pickerSub').textContent = opt.sub || '手动指定输出地址';
  state.picker.onConfirm = opt.onConfirm;
  $('pickerRoots').innerHTML = state.roots.map((r) =>
    '<button class="chip" data-p="' + esc(r.path) + '">' + esc(r.name) + '</button>').join('');
  $('pickerRoots').querySelectorAll('[data-p]').forEach((c) => c.onclick = () => loadPicker(c.dataset.p));
  $('pickerModal').classList.remove('hidden');
  loadPicker(opt.cwd || state.cwd || '');
}
function closePicker() { $('pickerModal').classList.add('hidden'); state.picker.onConfirm = null; }

/* ---------------- 冲突确认 ---------------- */
let conflictResolve = null;
function askConflict(items) {
  return new Promise((resolve) => {
    conflictResolve = resolve;
    $('confirmText').textContent = '有 ' + items.length + ' 个目标文件已存在，请选择处理方式：';
    $('conflictList').innerHTML = items.map((x) => '<div title="' + esc(x.output) + '">' + esc(x.output) + '</div>').join('');
    $('confirmModal').classList.remove('hidden');
  });
}
function finishConflict(v) {
  $('confirmModal').classList.add('hidden');
  if (conflictResolve) { conflictResolve(v); conflictResolve = null; }
}

/* ---------------- 转换执行 ---------------- */
function updateProgress(done, total, text) {
  $('progressFill').style.width = total ? (done / total * 100) + '%' : '0%';
  $('progressCount').textContent = done + ' / ' + total;
  $('progressText').textContent = text || '就绪';
}
async function startConvert() {
  if (state.running || !state.queue.length) return;
  const pending = state.queue.filter((q) => q.status !== 'done');
  if (!pending.length) { toast('队列中已没有待转换的文件', 'warn'); return; }

  let onExists = state.settings.onExists || 'ask';
  try {
    const chk = await post('/api/check', {
      items: pending.map((q) => ({ input: q.input, outputDir: q.outputDir, outputName: q.outputName })),
    });
    const conflicts = (chk.items || []).filter((x) => x.exists);
    if (conflicts.length && onExists === 'ask') {
      const decision = await askConflict(conflicts);
      if (!decision) { log('用户取消转换', 'warn'); return; }
      onExists = decision;
    }
  } catch (e) { toast('检查输出路径失败：' + e.message, 'err'); return; }

  state.running = true;
  $('btnConvert').disabled = true;
  let ok = 0, fail = 0, skip = 0;
  const total = pending.length;
  log('开始转换 ' + total + ' 个文件', 'ok');
  for (let k = 0; k < total; k++) {
    const q = pending[k];
    q.status = 'running'; q.message = ''; q.result = '';
    renderQueue();
    updateProgress(k, total, '正在转换：' + q.name);
    const output = joinPath(q.outputDir, q.outputName);
    try {
      const r = await post('/api/convert', {
        input: q.input, output,
        engine: state.settings.engine || 'auto',
        odaPath: state.settings.odaPath || '',
        dwgVersion: $('dwgVersion').value,
        dxfVersion: $('dxfVersion').value,
        odaVersion: $('odaVersion').value,
        onExists,
      });
      (r.logs || []).forEach((l) => log('  ' + l));
      if (r.ok) {
        if (r.skipped) { q.status = 'skipped'; q.message = '目标已存在，已跳过'; q.result = 'skipped'; skip++; }
        else {
          q.status = 'done'; q.result = r.output; q.message =
            '已生成 ' + fmtSize(r.size) + ' · ' + (r.engine === 'oda' ? 'ODA' : 'LibreDWG') +
            (r.version ? ' · ' + r.version : '');
          state.lastOutputDir = q.outputDir;
          ok++;
        }
        log('✓ ' + q.name + ' → ' + (r.output || output), 'ok');
      } else {
        q.status = 'failed'; q.message = r.error || '转换失败'; q.result = ''; fail++;
        log('✗ ' + q.name + '：' + q.message, 'err');
      }
    } catch (e) {
      q.status = 'failed'; q.message = e.message; fail++;
      log('✗ ' + q.name + '：' + e.message, 'err');
    }
    renderQueue();
    updateProgress(k + 1, total, '已完成 ' + (k + 1) + ' / ' + total);
  }
  state.running = false;
  $('btnConvert').disabled = false;
  updateProgress(total, total, '转换结束：成功 ' + ok + ' · 跳过 ' + skip + ' · 失败 ' + fail);
  toast('转换结束：成功 ' + ok + ' 个' + (fail ? '，失败 ' + fail + ' 个' : ''), fail ? 'warn' : 'ok');
  $('logPanel').classList.remove('hidden');
}

/* ---------------- 设置 / 初始化 ---------------- */
const VERSION_LABELS = {
  auto: '自动（跟随源 DWG，推荐）',
  r12: 'R12', r14: 'R14', r2000: 'R2000',
  r2004: 'R2004', r2007: 'R2007', r2010: 'R2010',
  r2013: 'R2013', r2018: 'R2018',
};
function fillSelect(id, values, def) {
  const el = $(id);
  el.innerHTML = (values || []).map((v) =>
    '<option value="' + esc(v) + '">' + esc(VERSION_LABELS[v] || v) + '</option>').join('');
  if (def && values.includes(def)) el.value = def;
}
async function loadEngines() {
  try {
    const d = await get('/api/engines');
    state.engines = d.status || {};
    state.versions = d.versions || state.versions;
    state.settings = Object.assign({}, d.settings || {});
    const libre = state.engines.libredwg, oda = state.engines.oda;
    $('engineDot').className = 'dot ' + (libre && libre.available ? 'ok' : 'warn');
    $('engineText').textContent = libre && libre.available
      ? (oda && oda.available ? '引擎就绪：LibreDWG + ODA' : '引擎就绪：内置 LibreDWG')
      : '内置引擎缺失';
    $('libreState').textContent = libre && libre.available ? '可用' : '不可用';
    $('dotLibre').className = 'dot ' + (libre && libre.available ? 'ok' : 'warn');
    $('odaState').textContent = oda && oda.available ? '已检测到' : '未安装（可选）';
    $('dotOda').className = 'dot ' + (oda && oda.available ? 'ok' : '');
    $('odaPathInput').value = state.settings.odaPath || '';
    fillSelect('dxfVersion', state.versions.dxf, state.settings.dxfVersion || 'auto');
    fillSelect('dwgVersion', state.versions.dwg, state.settings.dwgVersion || 'r2000');
    fillSelect('odaVersion', state.versions.oda, state.settings.odaVersion || 'ACAD2018');
    $('engineSelect').value = state.settings.engine || 'auto';
    $('onExists').value = state.settings.onExists || 'ask';
  } catch (e) { toast('引擎状态读取失败：' + e.message, 'err'); }
}
async function saveSettings(extra) {
  const s = Object.assign({
    engine: $('engineSelect').value,
    onExists: $('onExists').value,
    dxfVersion: $('dxfVersion').value,
    dwgVersion: $('dwgVersion').value,
    odaVersion: $('odaVersion').value,
    odaPath: $('odaPathInput').value.trim(),
  }, extra || {});
  Object.assign(state.settings, s);
  try { await post('/api/settings', s); } catch (e) { /* 忽略 */ }
}

function bind() {
  $('btnUp').onclick = () => {
    if (!state.cwd) return;
    const p = dirname(state.cwd.replace(/[\\/]$/, ''));
    loadList(p === state.cwd ? '' : p);
  };
  $('btnRoots').onclick = () => loadList('');
  $('btnRefresh').onclick = () => loadList(state.cwd);
  $('pathInput').onkeydown = (e) => { if (e.key === 'Enter') loadList($('pathInput').value.trim()); };
  $('btnAddQueue').onclick = () => addPaths([...state.selected]);
  $('btnPaste').onclick = () => {
    const v = $('pasteInput').value.trim().replace(/^"|"$/g, '');
    if (v) addPaths([v]);
    $('pasteInput').value = '';
  };
  $('pasteInput').onkeydown = (e) => { if (e.key === 'Enter') $('btnPaste').click(); };
  $('btnClearAll').onclick = () => {
    if (state.running) return;
    state.queue = []; state.active = -1; renderQueue(); setActive(-1);
  };
  $('btnClearDone').onclick = () => {
    if (state.running) return;
    state.queue = state.queue.filter((q) => q.status !== 'done');
    state.active = Math.min(state.active, state.queue.length - 1);
    renderQueue();
  };

  document.querySelectorAll('input[name="outMode"]').forEach((r) => r.onchange = () => {
    if (r.value === 'same' && r.checked) {
      state.customDir = null;
      state.queue.forEach((q) => { q.outputDir = dirname(q.input); q.isCustom = false; });
      $('customDirBox').classList.add('hidden');
      renderQueue();
      toast('已恢复默认：输出到源文件所在文件夹', 'ok');
    } else if (r.value === 'custom' && r.checked) {
      $('customDirBox').classList.remove('hidden');
      if (!state.customDir) $('btnPickDir').click();
    }
  });
  $('btnPickDir').onclick = () => openPicker({
    title: '选择输出地址', sub: '为队列中的全部文件统一指定输出文件夹',
    cwd: state.customDir || state.cwd || '',
    onConfirm: (dir) => {
      state.customDir = dir;
      $('customDirText').textContent = dir;
      state.queue.forEach((q) => { q.outputDir = dir; q.isCustom = true; });
      renderQueue();
      toast('输出地址已设置为：' + dir, 'ok');
    },
  });
  $('btnApplySuffix').onclick = () => {
    state.suffix = $('suffixInput').value.trim();
    state.queue.forEach((q) => {
      if (q.nameEdited) return;
      const outExt = q.ext === '.dwg' ? '.dxf' : '.dwg';
      const stem = q.name.slice(0, q.name.length - q.ext.length);
      q.outputName = stem + state.suffix + outExt;
    });
    renderQueue();
    toast(state.suffix ? '已应用后缀：' + state.suffix : '已恢复完全同名', 'ok');
  };
  $('suffixInput').onkeydown = (e) => { if (e.key === 'Enter') $('btnApplySuffix').click(); };

  ['engineSelect', 'onExists', 'dxfVersion', 'dwgVersion', 'odaVersion'].forEach((id) =>
    $(id).onchange = () => saveSettings());

  $('dxfVersion').addEventListener('change', () => {
    if (['r12', 'r14', 'r2000'].includes($('dxfVersion').value)) {
      toast('R2000 及以下会清空块定义（标注图形丢失），工具将自动改用源图纸版本重出', 'warn');
    }
  });

  $('btnZoomIn').onclick = () => { state.zoom = Math.min(state.zoom * 1.25, 8); applyTransform(); };
  $('btnZoomOut').onclick = () => { state.zoom = Math.max(state.zoom / 1.25, 0.2); applyTransform(); };
  $('btnZoomFit').onclick = fitPreview;
  const stage = $('previewStage');
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    state.zoom = Math.max(0.2, Math.min(8, state.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    applyTransform();
  }, { passive: false });
  let drag = null;
  stage.addEventListener('mousedown', (e) => {
    drag = { x: e.clientX, y: e.clientY, px: state.panX, py: state.panY };
    stage.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    state.panX = drag.px + e.clientX - drag.x;
    state.panY = drag.py + e.clientY - drag.y;
    applyTransform();
  });
  window.addEventListener('mouseup', () => { drag = null; stage.classList.remove('dragging'); });

  $('btnToggleLog').onclick = () => $('logPanel').classList.toggle('hidden');
  $('btnClearLog').onclick = () => { $('logBody').innerHTML = ''; };
  $('btnOpenOut').onclick = async () => {
    const p = state.lastOutputDir || (state.queue[0] ? state.queue[0].outputDir : '') || state.cwd;
    if (!p) { toast('还没有可打开的输出目录', 'warn'); return; }
    try { await post('/api/reveal', { path: p }); } catch (e) { toast(e.message, 'err'); }
  };
  $('btnConvert').onclick = startConvert;

  // picker
  $('btnPickerClose').onclick = closePicker;
  $('btnPickerCancel').onclick = closePicker;
  $('btnPickerUp').onclick = () => loadPicker(dirname(state.picker.cwd.replace(/[\\/]$/, '')) || '');
  $('btnPickerGo').onclick = () => loadPicker($('pickerPathInput').value.trim());
  $('pickerPathInput').onkeydown = (e) => { if (e.key === 'Enter') $('btnPickerGo').click(); };
  $('btnPickerConfirm').onclick = () => {
    const dir = state.picker.cwd;
    if (!dir) { toast('请先进入一个文件夹', 'warn'); return; }
    const cb = state.picker.onConfirm;
    closePicker();
    if (cb) cb(dir);
  };
  $('pickerModal').addEventListener('click', (e) => { if (e.target === $('pickerModal')) closePicker(); });

  // settings
  $('btnSettings').onclick = () => { loadEngines(); $('settingsModal').classList.remove('hidden'); };
  $('engineBadge').onclick = () => { loadEngines(); $('settingsModal').classList.remove('hidden'); };
  $('btnSettingsClose').onclick = () => $('settingsModal').classList.add('hidden');
  $('btnSettingsDone').onclick = async () => { await saveSettings(); $('settingsModal').classList.add('hidden'); };
  $('btnSaveOda').onclick = async () => {
    await saveSettings();
    await loadEngines();
    toast('已保存，引擎状态已刷新', 'ok');
  };
  $('settingsModal').addEventListener('click', (e) => { if (e.target === $('settingsModal')) $('settingsModal').classList.add('hidden'); });

  // help
  $('btnHelp').onclick = () => $('helpModal').classList.remove('hidden');
  $('btnHelpClose').onclick = () => $('helpModal').classList.add('hidden');
  $('btnHelpDone').onclick = () => $('helpModal').classList.add('hidden');
  $('helpModal').addEventListener('click', (e) => { if (e.target === $('helpModal')) $('helpModal').classList.add('hidden'); });

  // conflict
  $('btnConfirmCancel').onclick = () => finishConflict(null);
  $('btnConfirmSkip').onclick = () => finishConflict('skip');
  $('btnConfirmRename').onclick = () => finishConflict('rename');
  $('btnConfirmOverwrite').onclick = () => finishConflict('overwrite');
}

async function init() {
  bind();
  bindDrop();
  await loadEngines();
  try {
    const d = await get('/api/roots');
    state.roots = d.roots || [];
    renderRoots();
    const last = localStorage.getItem('cad_last_dir');
    const home = (state.roots.find((r) => r.name === '用户目录') || {}).path;
    await loadList(last || home || state.roots[0]?.path || '');
  } catch (e) { toast('初始化失败：' + e.message, 'err'); }
  renderQueue();
  updateProgress(0, 0, '就绪');
  log('工具已就绪，请从左侧选择 DXF / DWG 文件');
}

init();

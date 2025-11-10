const $ = (sel) => document.querySelector(sel);

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// --- View mode (list or grid) ---
let viewMode = (localStorage.getItem('viewMode') || 'grid');
function setViewMode(mode) {
  viewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('viewMode', viewMode);
  const wrap = document.getElementById('viewModeControl');
  const sw = document.getElementById('viewSwitch');
  if (wrap) wrap.classList.toggle('grid-on', viewMode === 'grid');
  if (sw) sw.checked = (viewMode === 'grid');
}

// Use page-specific renderer when available
function rerender(offset = 0) {
  if (typeof window.pageRender === 'function') {
    return window.pageRender(offset);
  }
  return render(offset);
}

// --- Hover preview (PDF first page or EPUB cover) ---
let _previewEl;
let _previewTimer;
const _previewCache = new Map(); // id -> HTMLElement (canvas or img)

function setupHoverPreview() {
  _previewEl = document.createElement('div');
  _previewEl.className = 'hover-preview';
  _previewEl.innerHTML = '<div class="inner"></div><div class="hint"></div>';
  document.body.appendChild(_previewEl);
}

function positionPreview(x, y) {
  if (!_previewEl) return;
  const pad = 12;
  const w = _previewEl.offsetWidth || 260;
  const h = _previewEl.offsetHeight || 340;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = x + 16;
  let top = y + 16;
  if (left + w + pad > vw) left = Math.max(pad, x - w - 16);
  if (top + h + pad > vh) top = Math.max(pad, y - h - 16);
  _previewEl.style.left = left + 'px';
  _previewEl.style.top = top + 'px';
}

function showPreviewSkeleton(msg = 'Loading preview…') {
  if (!_previewEl) return;
  _previewEl.querySelector('.inner').innerHTML = '<div style="padding:18px 12px; color: var(--muted)">' + msg + '</div>';
  _previewEl.querySelector('.hint').textContent = '';
  _previewEl.style.display = 'block';
}

function setPreviewContent(el, hintText = '') {
  if (!_previewEl) return;
  const inner = _previewEl.querySelector('.inner');
  inner.innerHTML = '';
  inner.appendChild(el);
  _previewEl.querySelector('.hint').textContent = hintText || '';
  _previewEl.style.display = 'block';
}

function hidePreview() {
  if (_previewEl) _previewEl.style.display = 'none';
  if (_previewTimer) { clearTimeout(_previewTimer); _previewTimer = null; }
}

function attachHoverPreview(targetEl, item) {
  let lastX = 0, lastY = 0;
  targetEl.addEventListener('mousemove', (e) => {
    lastX = e.clientX; lastY = e.clientY;
    if (_previewEl && _previewEl.style.display === 'block') positionPreview(lastX, lastY);
  });
  targetEl.addEventListener('mouseenter', () => {
    if (_previewTimer) clearTimeout(_previewTimer);
    _previewTimer = setTimeout(async () => {
      positionPreview(lastX, lastY);
      const id = item.id;
      const ext = (item.ext || '').toLowerCase();
      if (_previewCache.has(id)) {
        setPreviewContent(_previewCache.get(id).cloneNode(true));
        return;
      }
      showPreviewSkeleton(ext === 'pdf' ? 'Rendering first page…' : (ext === 'epub' ? 'Fetching cover…' : 'Preview unavailable'));
      try {
        let contentEl = null;
        if (ext === 'pdf') { await ensurePdfReady(); if (window.pdfjsLib) contentEl = await renderPdfFirstPage(`/file/${id}`); }
        else if (ext === 'epub') { await ensureEpubReady(); if (window.ePub) contentEl = await renderEpubCover(`/file/${id}`); }
        if (contentEl) {
          const node = makeReusableNode(contentEl);
          _previewCache.set(id, node.cloneNode(true));
          setPreviewContent(node, ext === 'pdf' ? 'First page' : 'Cover');
        } else {
          showPreviewSkeleton('No preview available');
        }
      } catch (e) {
        showPreviewSkeleton('Failed to load preview');
      }
    }, 180);
  });
  targetEl.addEventListener('mouseleave', () => hidePreview());
}

// Turn a canvas into an <img> so it can be reused and cloned reliably
function makeReusableNode(node){
  if (!node) return node;
  if (node.tagName && node.tagName.toLowerCase() === 'canvas'){
    try {
      const img = document.createElement('img');
      img.src = node.toDataURL('image/png');
      img.style.maxWidth = '100%';
      img.style.maxHeight = '100%';
      return img;
    } catch(_){ return node; }
  }
  return node;
}

// Wait briefly for vendor libs if not yet ready
function waitFor(condFn, timeout=1500, interval=60){
  return new Promise((resolve)=>{
    const start = Date.now();
    const tick = () => {
      if (condFn()) return resolve(true);
      if (Date.now() - start >= timeout) return resolve(false);
      setTimeout(tick, interval);
    };
    tick();
  });
}
async function ensurePdfReady(){ await waitFor(()=> !!window.pdfjsLib); }
async function ensureEpubReady(){ await waitFor(()=> !!window.ePub); }

// --- Thumbnail grid support ---
const _thumbCache = new Map(); // id -> HTMLElement (canvas/img)
const _thumbItems = new WeakMap(); // element -> item
let _thumbObserver;
function setupThumbObserver() {
  if (_thumbObserver) return;
  _thumbObserver = new IntersectionObserver((entries) => {
    entries.forEach(async (entry) => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      _thumbObserver.unobserve(el);
      const item = _thumbItems.get(el);
      if (!item) return;
      const id = item.id;
      if (_thumbCache.has(id)) {
        const node = _thumbCache.get(id).cloneNode(true);
        el.innerHTML = '';
        el.appendChild(node);
        return;
      }
      try {
        const ext = (item.ext || '').toLowerCase();
        let node = null;
        if (ext === 'pdf'){ await ensurePdfReady(); if (window.pdfjsLib) node = await renderPdfFirstPage(`/file/${id}`); }
        else if (ext === 'epub'){ await ensureEpubReady(); if (window.ePub) node = await renderEpubCover(`/file/${id}`); }
        if (node) {
          node = makeReusableNode(node);
          _thumbCache.set(id, node.cloneNode(true));
          el.innerHTML = '';
          el.appendChild(node);
        } else {
          el.textContent = 'No thumbnail';
        }
      } catch (e) {
        el.textContent = 'Failed to load';
      }
    });
  }, { root: null, rootMargin: '200px 0px', threshold: 0.1 });
}

function itemCard(item) {
  const li = document.createElement('li');
  li.className = 'card';
  li.dataset.id = String(item.id);
  li.dataset.ext = (item.ext || '').toLowerCase();
  // top-right star
  const head = document.createElement('div');
  head.className = 'head';
  const spacer = document.createElement('div');
  const star = document.createElement('button');
  star.className = 'star' + (item.starred ? ' on' : '');
  star.setAttribute('aria-label', 'Star');
  star.dataset.id = item.id;
  star.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.62L12 2 9.19 8.62 2 9.24l5.46 4.73L5.82 21z"/></svg>';
  star.title = item.starred ? 'Unstar' : 'Star';
  star.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    try {
      const wantOn = !(item.starred);
      const url = wantOn ? `/api/star/${item.id}?on=1` : `/api/star/${item.id}?off=1`;
      const res = await fetch(url);
      if (res.ok) { await render(); }
    } catch {}
  };
  head.appendChild(spacer);
  head.appendChild(star);
  li.appendChild(head);
  // thumbnail wrapped in link
  const link = document.createElement('a');
  link.href = `/view/${item.id}`;
  link.title = item.path;
  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  thumb.textContent = 'Loading…';
  link.appendChild(thumb);
  li.appendChild(link);
  // title link
  const title = document.createElement('a');
  title.href = `/view/${item.id}`;
  title.className = 'title';
  title.textContent = item.name;
  li.appendChild(title);
  // lazy-load thumb
  setupThumbObserver();
  _thumbItems.set(thumb, item);
  _thumbObserver.observe(thumb);
  // hover preview over card
  attachHoverPreview(li, item);
  return li;
}

async function renderPdfFirstPage(url) {
  const task = pdfjsLib.getDocument(url);
  const doc = await task.promise;
  const page = await doc.getPage(1);
  const vp = page.getViewport({ scale: 1 });
  const targetWidth = 240;
  const scale = Math.max(0.2, Math.min(4, targetWidth / vp.width));
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement('canvas');
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
  return canvas;
}

async function renderEpubCover(url) {
  try {
    // Load as ArrayBuffer to ensure epub.js treats it as an archive,
    // avoiding relative requests like /file/META-INF/container.xml
    const res = await fetch(url);
    const buf = await res.arrayBuffer();
    const book = ePub(buf);
    if (book.coverUrl) {
      const coverUrl = await book.coverUrl();
      if (coverUrl) {
        const img = new Image();
        img.decoding = 'async';
        img.src = coverUrl;
        await new Promise((res, rej)=>{ img.onload=()=>res(); img.onerror=()=>res(); });
        try { await book.destroy(); } catch(_) {}
        return img;
      }
    }
    // Fallback: try cover href via loaded.cover
    try {
      const coverHref = await book.loaded.cover;
      if (coverHref && book.archive && book.archive.getURL) {
        const url2 = await book.archive.getURL(coverHref);
        if (url2) {
          const img = new Image();
          img.decoding = 'async';
          img.src = url2;
          await new Promise((res)=>{ img.onload=()=>res(); img.onerror=()=>res(); });
          try { await book.destroy(); } catch(_) {}
          return img;
        }
      }
    } catch(_) {}
    try { await book.destroy(); } catch(_) {}
  } catch(_) {}
  return null;
}

async function search(q, ext, smart, offset = 0, limit = 50) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (ext) params.set('ext', ext);
  if (smart) params.set('smart', '1');
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const res = await fetch(`/api/search?${params.toString()}`);
  if (!res.ok) throw new Error('search failed');
  return res.json();
}

async function recent(offset = 0, limit = 50) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const res = await fetch(`/api/recent?${params.toString()}`);
  if (!res.ok) throw new Error('recent failed');
  return res.json();
}

async function triggerScan() {
  $('#status').textContent = 'Starting scan…';
  try {
    await fetch('/api/scan/start');
    pollScanStatus();
  } catch (e) {
    $('#status').textContent = 'Failed to start scan';
  }
}

let scanTimer;
async function pollScanStatus() {
  try {
    const res = await fetch('/api/scan/status');
    const data = await res.json();
    if (data.running) {
      if (data.percent != null && data.total) {
        $('#status').textContent = `Scanning… ${data.percent}% (${data.processed}/${data.total})`;
      } else {
        $('#status').textContent = 'Scanning…';
      }
      clearTimeout(scanTimer);
      scanTimer = setTimeout(pollScanStatus, 1500);
    } else if (data.result) {
      const r = data.result;
      if (r.error) {
        $('#status').textContent = 'Scan error: ' + r.error;
      } else {
        $('#status').textContent = `Scanned ${r.scanned}, updated ${r.updated || r.fts || 0}.`;
      }
      await render();
    } else {
      $('#status').textContent = 'Idle';
    }
  } catch (e) {
  $('#status').textContent = 'Scan status unavailable';
  }
}

async function triggerEnrichAll() {
  $('#status').textContent = 'Enriching all… (this may take a while)';
  try {
    const res = await fetch('/api/enrich/all');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'enrich failed');
    $('#status').textContent = `Enriched: updated ${data.updated} of ${data.processed} (missing ${data.missing}, denied ${data.denied}).`;
    await render();
  } catch (e) {
  $('#status').textContent = 'Enrich all failed';
  }
}

function itemRow(item) {
  const li = document.createElement('li');
  li.className = 'row';
  li.dataset.id = String(item.id);
  li.dataset.ext = (item.ext || '').toLowerCase();
  const star = document.createElement('button');
  star.className = 'star' + (item.starred ? ' on' : '');
  star.setAttribute('aria-label', 'Star');
  star.dataset.id = item.id;
  star.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.62L12 2 9.19 8.62 2 9.24l5.46 4.73L5.82 21z"/></svg>';
  star.title = item.starred ? 'Unstar' : 'Star';
  star.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    try {
      const wantOn = !(item.starred);
      const url = wantOn ? `/api/star/${item.id}?on=1` : `/api/star/${item.id}?off=1`;
      const res = await fetch(url);
      if (res.ok) { await render(); }
    } catch {}
  };
  const link = document.createElement('a');
  link.className = 'name';
  link.href = `/view/${item.id}`;
  link.textContent = item.name;
  link.title = item.path;
  const ext = document.createElement('span');
  ext.className = 'ext';
  ext.textContent = (item.ext || '').toUpperCase();
  const size = document.createElement('span');
  size.className = 'size';
  const sizeMB = (item.size / (1024*1024));
  size.textContent = (sizeMB >= 100 ? sizeMB.toFixed(0) : sizeMB.toFixed(2)) + ' MB';
  li.appendChild(star);
  li.appendChild(link);
  li.appendChild(ext);
  li.appendChild(size);
  const dl = document.createElement('a');
  dl.href = `/file/${item.id}`;
  dl.textContent = ' ⤓';
  dl.title = 'Download';
  dl.style.marginLeft = '6px';
  li.appendChild(dl);
  if (item.snippet) {
    const snip = document.createElement('div');
    snip.className = 'snippet';
    snip.innerHTML = item.snippet.replaceAll('[','<mark>').replaceAll(']','</mark>');
    li.appendChild(snip);
  }
  // Hover preview bindings on the whole row
  attachHoverPreview(li, item);
  return li;
}

async function render(offset = 0) {
  const q = $('#q').value.trim();
  const ext = $('#ext').value;
  const smart = ($('#smart')?.checked ?? true);
  const data = await search(q, ext, smart, offset);
  const list = $('#results');
  list.classList.toggle('grid', viewMode === 'grid');
  list.innerHTML = '';
  if (viewMode === 'grid') {
    data.items.forEach(x => list.appendChild(itemCard(x)));
  } else {
    data.items.forEach(x => list.appendChild(itemRow(x)));
  }
  $('#status').textContent = `${data.total} result(s)` + (smart ? ' • smart' : '');
}

async function renderRecent(offset = 0) {
  const data = await recent(offset);
  const list = $('#results');
  list.classList.toggle('grid', viewMode === 'grid');
  list.innerHTML = '';
  if (viewMode === 'grid') {
    data.items.forEach(x => list.appendChild(itemCard(x)));
  } else {
    data.items.forEach(x => list.appendChild(itemRow(x)));
  }
  $('#status').textContent = `${data.total} recently viewed`;
}

document.addEventListener('DOMContentLoaded', () => {
  setupHoverPreview();
  // Initialize view toggle
  setViewMode(viewMode);
  const viewSwitch = document.getElementById('viewSwitch');
  if (viewSwitch) viewSwitch.addEventListener('change', (e) => { setViewMode(viewSwitch.checked ? 'grid' : 'list'); rerender(); });
  const viewCtl = document.getElementById('viewModeControl');
  if (viewCtl) viewCtl.addEventListener('click', (e) => {
    const sw = document.getElementById('viewSwitch');
    const target = e.target;
    if (target.classList && target.classList.contains('left')) {
      setViewMode('list'); if (sw) sw.checked = false; rerender(); return;
    }
    if (target.classList && target.classList.contains('right')) {
      setViewMode('grid'); if (sw) sw.checked = true; rerender(); return;
    }
    if (target.classList && target.classList.contains('slider')) {
      const nextOn = !(sw && sw.checked);
      setViewMode(nextOn ? 'grid' : 'list'); if (sw) sw.checked = nextOn; rerender(); return;
    }
  });
  const debounced = debounce(() => rerender(), 180);
  const qEl = $('#q'); if (qEl) qEl.addEventListener('input', debounced);
  const extEl = $('#ext'); if (extEl) extEl.addEventListener('change', () => rerender());
  const smartEl = $('#smart'); if (smartEl) smartEl.addEventListener('change', () => rerender());
  const scanBtn = $('#scan'); if (scanBtn) scanBtn.addEventListener('click', () => triggerScan());
  const enrichBtn = $('#enrichAll'); if (enrichBtn) enrichBtn.addEventListener('click', () => triggerEnrichAll());
  // Keyboard shortcut: Ctrl+/ (or Cmd+/) to focus search
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === '/') {
      e.preventDefault();
      const q = $('#q'); if (q) { q.focus(); q.select(); }
    }
  });
  // Clicking title goes "home" (clear filters and search)
  const home = document.getElementById('homeLink');
  if (home) home.addEventListener('click', (e) => {
    e.preventDefault();
    const q = $('#q'); if (q) q.value = '';
    const ext = $('#ext'); if (ext) ext.value = '';
    const smart = $('#smart'); if (smart) smart.checked = true;
    render();
  });
  rerender();
});

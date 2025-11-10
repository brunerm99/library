from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
from typing import Optional

from .logutil import logger, get_recent_logs, configure_logger

from .config import AppConfig, load_config
from .db import (
    connect,
    init_db,
    ensure_fts,
    search as db_search,
    smart_search as db_smart_search,
    get_item,
)
from .scanner import scan_into


class LibraryHandler(SimpleHTTPRequestHandler):
    cfg: AppConfig
    db_path: Path
    scan_mgr: "ScanManager"

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/search"):
            return self.handle_search()
        if self.path.startswith("/api/log"):
            return self.handle_log()
        if self.path.startswith("/api/logs"):
            return self.handle_logs()
        if self.path.startswith("/api/scan/start"):
            return self.handle_scan_start()
        if self.path.startswith("/api/scan/status"):
            return self.handle_scan_status()
        if self.path.startswith("/api/scan"):
            # Back-compat: start in background and return status
            return self.handle_scan_start()
        if self.path.startswith("/api/item/"):
            return self.handle_item()
        if self.path.startswith("/api/enrich/"):
            return self.handle_enrich()
        if self.path.startswith("/api/enrich_all") or self.path.startswith("/api/enrich/all"):
            return self.handle_enrich_all()
        if self.path.startswith("/api/star/"):
            return self.handle_star()
        if self.path.startswith("/view/"):
            return self.handle_view()
        if self.path.startswith("/file/"):
            return self.handle_file()
        # serve static UI from web/
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/log"):
            return self.handle_log()
        # Unsupported
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method")
        return

    def log_message(self, format: str, *args):  # noqa: A003
        try:
            msg = format % args if args else format
            logger.info(msg)
        except Exception:
            return super().log_message(format, *args)

    def translate_path(self, path: str) -> str:
        # Map to package's ./web for static assets without exposing FS
        web_root = Path(__file__).resolve().parent / "web"
        if path == "/":
            return str(web_root / "index.html")
        # Prevent traversals and only serve from web
        safe = Path("/" + path.lstrip("/")).resolve().relative_to("/")
        return str(web_root / safe)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_log(self):
        try:
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                data = {}
                if raw:
                    try:
                        data = json.loads(raw.decode("utf-8", errors="ignore"))
                    except Exception:
                        data = {"raw": raw.decode("utf-8", errors="ignore")}
            else:
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                data = {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}
            level = str(data.get("level") or "info").lower()
            msg = data.get("msg") or data.get("message") or "client-log"
            meta = {k: v for k, v in data.items() if k not in {"level", "msg", "message"}}
            line = f"{msg} | {meta}" if meta else msg
            if level in {"warn", "warning"}:
                logger.warning(line)
            elif level in {"error", "err"}:
                logger.error(line)
            else:
                logger.info(line)
        except Exception as e:
            logger.exception("handle_log failed: {}", e)
        finally:
            self.send_response(204)
            self.end_headers()
            return

    def handle_logs(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        try:
            n = int((params.get("n") or ["200"])[0])
        except Exception:
            n = 200
        logs = get_recent_logs(n)
        return self._json_response({"lines": logs})

    def handle_search(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        q = (params.get("q") or [None])[0]
        ext = (params.get("ext") or [None])[0]
        limit = int((params.get("limit") or ["50"])[0])
        offset = int((params.get("offset") or ["0"])[0])
        conn = connect(self.db_path)
        smart = (params.get("smart") or ["0"])[0] in {"1", "true", "yes"}
        if smart and ensure_fts(conn):
            total, rows = db_smart_search(conn, q=q, ext=ext, limit=limit, offset=offset)
        else:
            total, rows = db_search(conn, q=q, ext=ext, limit=limit, offset=offset)
        data = {
            "total": total,
            "items": [dict(r) for r in rows],
            "smart": smart,
        }
        return self._json_response(data)

    def handle_item(self):
        try:
            item_id = int(self.path.rsplit("/", 1)[-1])
        except Exception:
            return self._json_response({"error": "invalid id"}, status=400)
        conn = connect(self.db_path)
        row = get_item(conn, item_id)
        if not row:
            return self._json_response({"error": "not found"}, status=404)
        return self._json_response(dict(row))

    def handle_star(self):
        from .db import set_star, is_starred
        try:
            path_only = self.path.split("?", 1)[0]
            # Expecting /api/star/<id>
            item_id = int(path_only.rsplit("/", 1)[-1])
        except Exception:
            return self._json_response({"error": "invalid id"}, status=400)
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        conn = connect(self.db_path)
        # Determine action
        def truthy(val: str) -> bool:
            return val.lower() in {"1", "true", "yes", "on"}
        if "on" in params:
            set_star(conn, item_id, truthy(params["on"][0]))
        elif "off" in params:
            set_star(conn, item_id, False)
        else:
            # toggle
            cur = is_starred(conn, item_id)
            set_star(conn, item_id, not cur)
        starred = is_starred(conn, item_id)
        # fetch time if starred
        starred_at = None
        if starred:
            row = connect(self.db_path).execute("SELECT starred_at FROM items_star WHERE id=?", (item_id,)).fetchone()
            starred_at = row[0] if row else None
        return self._json_response({"id": item_id, "starred": starred, "starred_at": starred_at})

    def handle_scan_start(self):
        started = self.scan_mgr.start(self.cfg, self.db_path)
        return self._json_response(self.scan_mgr.status() | {"started": started})

    def handle_scan_status(self):
        return self._json_response(self.scan_mgr.status())

    def handle_file(self):
        # /file/<id> serves the file if within allowed roots
        try:
            item_id = int(self.path.split("/", 2)[2])
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid file id")
            return
        conn = connect(self.db_path)
        row = get_item(conn, item_id)
        if not row:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        p = Path(row["path"]).resolve()
        if not any(str(p).startswith(str(r)) for r in self.cfg.normalized_roots()):
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside allowed roots")
            return
        if not p.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File missing")
            return
        data = p.read_bytes()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        # Force correct types for certain formats
        if p.suffix.lower() == '.epub':
            ctype = 'application/epub+zip'
        elif p.suffix.lower() == '.cbz':
            ctype = 'application/vnd.comicbook+zip'
        elif p.suffix.lower() == '.cbr':
            ctype = 'application/vnd.comicbook-rar'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Serve inline for types commonly viewable in browser or our viewers
        inline_exts = {"pdf", "txt", "html", "xhtml", "htm", "epub"}
        disp = "inline" if p.suffix.lower().lstrip(".") in inline_exts else "attachment"
        # Build ASCII-safe Content-Disposition with RFC 5987 filename*
        try:
            ascii_name = p.name.encode('ascii', 'ignore').decode('ascii')
            if not ascii_name:
                ascii_name = "file" + p.suffix
        except Exception:
            ascii_name = "file" + p.suffix
        quoted_utf8 = urllib.parse.quote(p.name, safe="")
        cd_value = f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted_utf8}"
        self.send_header("Content-Disposition", cd_value)
        self.end_headers()
        self.wfile.write(data)

    def handle_enrich(self):
        from .content import extract_text
        from .ai import heuristic_tags_and_summary
        from .db import upsert_items_fts, upsert_items_meta

        try:
            item_id = int(self.path.rsplit("/", 1)[-1])
        except Exception:
            return self._json_response({"error": "invalid id"}, status=400)
        conn = connect(self.db_path)
        row = get_item(conn, item_id)
        if not row:
            return self._json_response({"error": "not found"}, status=404)
        p = Path(row["path"])  # may be missing
        text = extract_text(p) if p.exists() else None
        tags, summary = heuristic_tags_and_summary(p, text)
        # always store meta
        import time as _t
        upsert_items_meta(conn, [(row["id"], tags, summary, _t.time())])
        fts = ensure_fts(conn)
        if fts:
            doc = (row["id"], row["name"], row["dir"], row["ext"], tags, summary, text or "")
            upsert_items_fts(conn, [doc])
        return self._json_response({
            "id": row["id"],
            "tags": tags,
            "summary": summary,
            "content_bytes": len((text or '').encode('utf-8')),
            "fts": fts,
        })

    def handle_view(self):
        try:
            item_id = int(self.path.split("/", 2)[2])
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid id")
            return
        conn = connect(self.db_path)
        row = get_item(conn, item_id)
        if not row:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        ext = row["ext"].lower()
        name = row["name"]
        file_url = f"/file/{row['id']}"
        logger.info(f"viewer open: id={item_id}, ext={ext}, name={name}")
        html = self._render_viewer(name, ext, file_url, item_id)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render_viewer(self, title: str, ext: str, file_url: str, item_id: int) -> str:
        safe_title = title.replace("<", "").replace(">", "")
        tpl_dir = Path(__file__).resolve().parent / "web"
        def load(name: str) -> str:
            return (tpl_dir / name).read_text(encoding="utf-8")
        def compose(tpl: str) -> str:
            return (tpl
                    .replace("__TITLE__", safe_title)
                    .replace("__FILE_URL__", file_url)
                    .replace("__FILE_URL_JSON__", json.dumps(file_url))
                    .replace("__ITEM_ID__", str(item_id)))
        if ext == "pdf":
            return compose(load("viewer_pdf.html"))
        if ext == "epub":
            return compose(load("viewer_epub.html"))
        if ext == "txt":
            tpl = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>__TITLE__</title></head><body><pre id=\"txt\">Loading…</pre><script>fetch(__FILE_URL_JSON__).then(r=>r.text()).then(t=>{document.getElementById('txt').textContent=t;}).catch(()=>{document.getElementById('txt').textContent='Failed to load text file.';});</script></body></html>"
            return compose(tpl)
        # Fallback embed
        return compose("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>__TITLE__</title></head><body><embed src=\"__FILE_URL__\" type=\"application/octet-stream\" /></body></html>")
        # Minimal viewers: PDF via iframe (browser PDF viewer), EPUB via EPUB.js, TXT via fetch.
        # Fallback shows download link.
        safe_title = title.replace("<", "").replace(">", "")
        base = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>{title}</title>
  <meta name=\"theme-color\" content=\"#1F262A\" />
  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\" />
  <style>
    /* Rosé Pine Moon */
    :root {{ --base:#1F262A; --surface:#51676D; --overlay:#2A3439; --text:#AAAAAD; --muted:#848B89; --accent:#E94560; }}
    html, body {{ height:100%; margin:0; background:var(--base); color:var(--text); }}
    .topbar {{ display:flex; align-items:center; justify-content:space-between; padding:8px 12px; background:var(--surface); border-bottom:1px solid rgba(224,222,244,.1); }}
    .title {{ font-weight:600; }}
    .actions a {{ color:var(--accent); text-decoration:none; }}
    .viewer {{ height:calc(100% - 46px); display:flex; flex-direction:column; }}
    iframe, embed {{ width:100%; height:100%; border:0; background:var(--overlay); }}
    #epub-view {{ width:100%; height:100%; }}
    .pdf-toolbar {{ display:flex; gap:8px; align-items:center; padding:6px 8px; background:var(--surface); border-bottom:1px solid rgba(224,222,244,.1); }}
    .pdf-toolbar .spacer {{ flex:1 1 auto; }}
    .pdf-toolbar button, .pdf-toolbar select {{ background:var(--overlay); color:var(--text); border:1px solid rgba(249,249,249,.12); border-radius:6px; padding:4px 8px; cursor:pointer; }}
    #pdf-container {{ flex:1 1 auto; overflow:auto; display:flex; justify-content:center; align-items:flex-start; background:var(--overlay); }}
    #pdf-canvas {{ max-width:100%; height:auto; }}
    pre {{ white-space:pre-wrap; padding:12px; }}
  </style>
  {head_extra}
  <script>
    const FILE_URL = {file_url!r};
    const ITEM_ID = {item_id};
    function reportLog(obj){
      try{
        const data = JSON.stringify(obj||{});
        if (navigator.sendBeacon) {
          const blob = new Blob([data], {type:'application/json'});
          navigator.sendBeacon('/api/log', blob);
        } else {
          fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'}, body: data});
        }
      } catch(_){/* ignore */}
    }
    window.addEventListener('error', (e)=>{
      try{ reportLog({level:'error', type:'viewer_js_error', message: String(e.message||''), file: e.filename, line: e.lineno, col: e.colno, id: ITEM_ID}); }catch(_){}
    });
  </script>
</head>
<body>
  <div class=\"topbar\">
    <div class=\"title\">{title}</div>
    <div class=\"actions\"><a href=\"{file_url}\" download>Download</a></div>
  </div>
  <div class=\"viewer\">
    {viewer}
  </div>
  {body_extra}
</body>
</html>
"""
        def _compose(head_extra: str, viewer: str, body_extra: str) -> str:
            html = base
            # Replace placeholders (both simple and repr-style)
            html = html.replace("{title}", safe_title)
            html = html.replace("{file_url}", file_url)
            try:
                html = html.replace("{file_url!r}", json.dumps(file_url))
            except Exception:
                html = html.replace("{file_url!r}", f'"{file_url}"')
            html = html.replace("{head_extra}", head_extra)
            html = html.replace("{viewer}", viewer)
            html = html.replace("{body_extra}", body_extra)
            html = html.replace("{item_id}", str(item_id))
            # Fix CSS braces originally doubled to escape format
            html = html.replace("{{", "{").replace("}}", "}")
            return html
        if ext == "pdf":
            head_extra = """
<script src=\"/vendor/pdfjs/pdf.min.js\"></script>
<script>
  if (window['pdfjsLib']) { pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.js'; }
  const _debounce = (fn, ms)=>{ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); } };
</script>
"""
            viewer = """
<div class=\"pdf-toolbar\">
  <button id=\"prev\" title=\"Previous page\">◀</button>
  <span id=\"pageLabel\">1 / ?</span>
  <button id=\"next\" title=\"Next page\">▶</button>
  <select id=\"chapter\" title=\"Chapters\"></select>
  <span class=\"spacer\"></span>
  <button id=\"fit\" title=\"Fit height\">Fit</button>
  <button id=\"zoomOut\" title=\"Zoom out\">−</button>
  <button id=\"zoomIn\" title=\"Zoom in\">＋</button>
</div>
<div id=\"pdf-container\"><div id=\"pdf-pages\"></div></div>
"""
            body_extra = r"""
<script>
let pdfDoc=null, pageNum=1, scale=1.0, autoFit=true, fitMode='height';
const KEY='pdfpos:'+ITEM_ID;
const container = document.getElementById('pdf-container');
const pagesEl = document.getElementById('pdf-pages');
function updatePageLabel(){ document.getElementById('pageLabel').textContent = pageNum+' / '+(pdfDoc?pdfDoc.numPages:'?'); }
function fitWidthScale(page){ const vw = page.getViewport({scale:1}); const cw = container.clientWidth||vw.width; return Math.max(0.5, Math.min(4, cw / vw.width)); }
function fitHeightScale(page){ const vw = page.getViewport({scale:1}); const ch = container.clientHeight||vw.height; return Math.max(0.5, Math.min(4, ch / vw.height)); }
function pageScale(page){ if(!autoFit) return scale||1; return (fitMode==='height')? fitHeightScale(page): fitWidthScale(page); }
function dragScrollable(el){ let down=false, sx=0, sy=0, st=0, sl=0; el.style.cursor='grab'; el.addEventListener('mousedown', (e)=>{ down=true; el.style.cursor='grabbing'; sx=e.clientX; sy=e.clientY; st=el.scrollTop; sl=el.scrollLeft; e.preventDefault(); }); window.addEventListener('mousemove', (e)=>{ if(!down) return; el.scrollTo({ top: st - (e.clientY-sy), left: sl - (e.clientX-sx), behavior:'auto' }); }); window.addEventListener('mouseup', ()=>{ down=false; el.style.cursor='grab'; }); }
dragScrollable(container);
function createPageView(n){ const wrap=document.createElement('div'); wrap.className='page'; wrap.dataset.page=n; wrap.style.display='flex'; wrap.style.justifyContent='center'; wrap.style.padding='10px 0'; const canvas=document.createElement('canvas'); canvas.dataset.rendered='0'; wrap.appendChild(canvas); return wrap; }
function savePdfState(){ try{ localStorage.setItem(KEY, JSON.stringify({page:pageNum, scale:scale, autoFit, fitMode})); }catch{} }
function renderPage(n){ if(!pdfDoc) return; const wrap = pagesEl.querySelector('[data-page="'+n+'"]'); if(!wrap) return; const canvas = wrap.querySelector('canvas'); if(canvas.dataset.rendering==='1') return; canvas.dataset.rendering='1'; pdfDoc.getPage(n).then(page=>{ const s = pageScale(page); const vp = page.getViewport({ scale: s }); canvas.width = vp.width; canvas.height = vp.height; page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise.then(()=>{ canvas.dataset.rendered='1'; canvas.dataset.rendering='0'; savePdfState(); }); }); }
function ensurePages(){ if(!pdfDoc) return; if(pagesEl.children.length) return; for(let i=1;i<=pdfDoc.numPages;i++){ pagesEl.appendChild(createPageView(i)); } }
let _visScore = {};
function onVisible(entries){
  entries.forEach(entry=>{
    const n = parseInt(entry.target.dataset.page);
    _visScore[n] = entry.isIntersecting ? (entry.intersectionRatio || 0) : 0;
    if(entry.isIntersecting){ renderPage(n); }
  });
  // Pick most visible page as current
  let best = pageNum, bestScore = -1;
  for (const [k,v] of Object.entries(_visScore)){
    const n = parseInt(k); if (v > bestScore){ bestScore=v; best=n; }
  }
  if (!isNaN(best) && best !== pageNum){ pageNum = best; updatePageLabel(); savePdfState(); }
  // Update chapter selection to current chapter start <= pageNum
  const sel = document.getElementById('chapter');
  if (sel && window.chapterStarts && window.chapterStarts.length){
    let chosen = String(window.chapterStarts[0]);
    for (const p of window.chapterStarts){ if (p <= pageNum) chosen = String(p); else break; }
    if (sel.value !== chosen) sel.value = chosen;
  }
}
const io = new IntersectionObserver(onVisible, { root: container, rootMargin: '150px 0px', threshold: [0.1,0.25,0.5,0.75] });
document.getElementById('prev').onclick=()=>{ if(pdfDoc && pageNum>1){ const n = pageNum-1; pageNum = n; const el=pagesEl.querySelector('[data-page="'+n+'"]'); if(el){ el.scrollIntoView({behavior:'auto', block:'start'}); } updatePageLabel(); savePdfState(); renderPage(n); }};
document.getElementById('next').onclick=()=>{ if(pdfDoc && pageNum<pdfDoc.numPages){ const n = pageNum+1; pageNum = n; const el=pagesEl.querySelector('[data-page="'+n+'"]'); if(el){ el.scrollIntoView({behavior:'auto', block:'start'}); } updatePageLabel(); savePdfState(); renderPage(n); }};
document.getElementById('zoomIn').onclick=()=>{ autoFit=false; scale=Math.min(4, (scale||1)*1.2); pagesEl.querySelectorAll('canvas').forEach(c=>c.dataset.rendered='0'); renderPage(pageNum); };
document.getElementById('zoomOut').onclick=()=>{ autoFit=false; scale=Math.max(0.5, (scale||1)/1.2); pagesEl.querySelectorAll('canvas').forEach(c=>c.dataset.rendered='0'); renderPage(pageNum); };
document.getElementById('fit').onclick=()=>{ autoFit=true; fitMode='height'; pagesEl.querySelectorAll('canvas').forEach(c=>c.dataset.rendered='0'); renderPage(pageNum); };
window.addEventListener('resize', _debounce(()=>{ if(autoFit){ pagesEl.querySelectorAll('canvas').forEach(c=>c.dataset.rendered='0'); renderPage(pageNum); } }, 150));
document.getElementById('chapter').onchange=()=>{ const val=document.getElementById('chapter').value; const n=parseInt(val||'1'); if(!isNaN(n)){ pageNum = Math.max(1, Math.min(pdfDoc.numPages, n)); const el=pagesEl.querySelector('[data-page="'+pageNum+'"]'); if(el){ el.scrollIntoView({behavior:'auto', block:'start'}); } updatePageLabel(); savePdfState(); renderPage(pageNum); }};
async function buildNamedDestMap(doc){
  try {
    if (typeof doc.getNamedDestinations === 'function') {
      const named = await doc.getNamedDestinations();
      const map = {};
      for (const k in named) {
        try { const ref = named[k][0]; const idx = await doc.getPageIndex(ref); map[k] = idx; } catch {}
      }
      return map;
    } else if (typeof doc.getDestinations === 'function') {
      const named = await doc.getDestinations();
      const map = {};
      for (const k in named) {
        try { const ref = named[k][0]; const idx = await doc.getPageIndex(ref); map[k] = idx; } catch {}
      }
      return map;
    }
  } catch {}
  return {};
}
function populateChaptersFromOutline(doc){ return doc.getOutline().then(async (outline)=>{ const sel=document.getElementById('chapter'); sel.innerHTML=''; const items=[]; function walk(nodes, depth){ (nodes||[]).forEach(n=>{ items.push({n, depth}); walk(n.items, depth+1); }); } walk(outline, 0); const added = new Set(); const starts=[]; const namedMap = await buildNamedDestMap(doc); if(items.length){ for(const {n, depth} of items){ let pageIndex=null; try{ if(n.dest){ let destArr = null; if(typeof n.dest === 'string'){ if (namedMap.hasOwnProperty(n.dest)) { pageIndex = namedMap[n.dest]; } else { const arr = await doc.getDestination(n.dest); if (arr) { const ref = arr[0]; pageIndex = await doc.getPageIndex(ref); } } } else if (Array.isArray(n.dest)) { const ref = n.dest[0]; pageIndex = await doc.getPageIndex(ref); } } else if (n.url && n.url.includes('#page=')) { const m = n.url.match(/#page=(\d+)/); if(m){ pageIndex = parseInt(m[1], 10) - 1; } } }catch(e){} if(pageIndex != null && pageIndex >= 0 && pageIndex < doc.numPages){ const pageNum1 = pageIndex + 1; if(!added.has(pageNum1)){ const opt=document.createElement('option'); opt.value=String(pageNum1); opt.textContent = ("\u00A0".repeat(depth*2)) + (n.title||('Page '+pageNum1)); sel.appendChild(opt); added.add(pageNum1); starts.push(pageNum1); } } } } if(!sel.children.length){ for(let i=1;i<=doc.numPages;i++){ const opt=document.createElement('option'); opt.value=String(i); opt.textContent='Page '+i; sel.appendChild(opt); starts.push(i); } } starts.sort((a,b)=>a-b); window.chapterStarts = starts; sel.value=String(pageNum); }); }
pdfjsLib.getDocument(FILE_URL).promise.then(async doc=>{ pdfDoc=doc; try{ const raw=localStorage.getItem(KEY); const saved = raw && raw.trim().startsWith('{') ? JSON.parse(raw) : {}; if(saved.page) pageNum=saved.page; if(saved.scale) scale=saved.scale; if(typeof saved.autoFit==='boolean') autoFit=saved.autoFit; if(saved.fitMode) fitMode=saved.fitMode; }catch{} updatePageLabel(); ensurePages(); pagesEl.querySelectorAll('.page').forEach(p=>io.observe(p)); await populateChaptersFromOutline(doc); const target = pagesEl.querySelector('[data-page="'+pageNum+'"]'); if(target){ target.scrollIntoView({behavior:'auto', block:'start'}); } renderPage(pageNum); });
window.addEventListener('beforeunload', savePdfState);
</script>
"""
            return _compose(head_extra, viewer, body_extra)
        if ext == "epub":
            head_extra = "<script src=\"/vendor/epub/epub.min.js\"></script>"
            viewer = """
<div class=\"pdf-toolbar\">
  <select id=\"epub-chapter\" title=\"Chapters\"></select>
  <span class=\"spacer\"></span>
  <button id=\"epub-prev\" title=\"Previous\">◀</button>
  <button id=\"epub-next\" title=\"Next\">▶</button>
</div>
<div id=\"epub-view\"></div>
"""
            body_extra = """
<script>
  const KEY = 'epubcfi:'+ITEM_ID;
  let book, rendition;
  function dragScrollable(el){ let down=false, sx=0, sy=0, st=0, sl=0; el.style.cursor='grab'; el.addEventListener('mousedown', (e)=>{ down=true; el.style.cursor='grabbing'; sx=e.clientX; sy=e.clientY; st=el.scrollTop; sl=el.scrollLeft; e.preventDefault(); }); window.addEventListener('mousemove', (e)=>{ if(!down) return; el.scrollTo({ top: st - (e.clientY-sy), left: sl - (e.clientX-sx), behavior:'auto' }); }); window.addEventListener('mouseup', ()=>{ down=false; el.style.cursor='grab'; }); }
  fetch(FILE_URL).then(r=>{ if(!r.ok){ throw new Error('HTTP '+r.status); } return r.arrayBuffer(); }).then(buf=>{
    try { book = ePub(buf); } catch(err) { try{ reportLog({level:'error', type:'epub_init_error', id: ITEM_ID, error: String(err)}); }catch(_){}; throw err; }
    rendition = book.renderTo('epub-view', { width: '100%', height: '100%' });
    try { rendition.flow('scrolled-doc'); } catch(e) { try { rendition.flow('scrolled'); } catch(_) { try{ reportLog({level:'warning', type:'epub_flow_fallback', id: ITEM_ID}); }catch(_){} } }
    rendition.themes.default({ 'body': { 'background': getComputedStyle(document.documentElement).getPropertyValue('--overlay').trim() || '#393552', 'color': getComputedStyle(document.documentElement).getPropertyValue('--text').trim() || '#e0def4' } });
    // Enable drag-to-scroll on the surrounding viewer
    const viewer = document.querySelector('.viewer'); if (viewer) dragScrollable(viewer);
    return book.ready.then(() => {
      const cfi = localStorage.getItem(KEY);
      if (cfi) { return rendition.display(cfi).catch((err)=>{ try{ reportLog({level:'warning', type:'epub_display_cfi_fallback', id: ITEM_ID, error: String(err)});}catch(_){}; return rendition.display(); }); } else { return rendition.display(); }
    }).then(() => {
      function saveEpub(){ try{ const loc = rendition.currentLocation(); if(loc && loc.start && loc.start.cfi){ localStorage.setItem(KEY, loc.start.cfi); } } catch{} }
      rendition.on('relocated', (loc)=>{ try { if(loc && loc.start && loc.start.cfi){ localStorage.setItem(KEY, loc.start.cfi); } const sel=document.getElementById('epub-chapter'); if(sel && loc.start && loc.start.href){ const idx = [...sel.options].findIndex(o=>o.value===loc.start.href); if(idx>=0) sel.selectedIndex = idx; } } catch {} });
      // Populate TOC
      return book.loaded.navigation.then(nav=>{
        const sel = document.getElementById('epub-chapter');
        sel.innerHTML='';
        (nav.toc||[]).forEach(item=>{ const canon = (book.path && book.path.resolve) ? book.path.resolve(item.href) : item.href; const opt=document.createElement('option'); opt.value=canon; opt.textContent=item.label || item.href; sel.appendChild(opt); });
        sel.onchange=()=>{ const href=sel.value; if(href) rendition.display(href).then(()=>saveEpub()).catch(err=>{ try{ reportLog({level:'error', type:'epub_display_href_error', id: ITEM_ID, href, error: String(err)});}catch(_){} }); };
        const prevBtn = document.getElementById('epub-prev');
        const nextBtn = document.getElementById('epub-next');
        if (prevBtn) prevBtn.onclick=()=>rendition.prev().then(()=>saveEpub());
        if (nextBtn) nextBtn.onclick=()=>rendition.next().then(()=>saveEpub());
      });
    });
  }).catch((err)=>{
    try { reportLog({level:'error', type:'epub_fetch_or_init_error', id: ITEM_ID, error: String(err)}); } catch(_){}
    document.getElementById('epub-view').innerHTML = '<p style="padding:12px">Failed to load EPUB.</p>';
  });
</script>
"""
            return _compose(head_extra, viewer, body_extra)
        if ext == "txt":
            viewer = "<pre id=\"txt\">Loading…</pre>"
            body_extra = """
<script>
  fetch(FILE_URL).then(r => r.text()).then(t => {
    document.getElementById('txt').textContent = t;
  }).catch(() => {
    document.getElementById('txt').textContent = 'Failed to load text file.';
  });
</script>
"""
            return _compose("", viewer, body_extra)
        # Fallback: let browser handle via inline/object, or provide download link
        return _compose("", f"<embed src=\"{file_url}\" type=\"application/octet-stream\" />", "")

    def handle_enrich_all(self):
        from .content import extract_text
        from .ai import heuristic_tags_and_summary
        from .db import upsert_items_fts, upsert_items_meta, items_freshness, fts_ids_present

        conn = connect(self.db_path)
        roots = [str(r) for r in self.cfg.normalized_roots()]
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
        processed = 0
        updated = 0
        missing = 0
        denied = 0
        skipped = 0
        batch = []
        batch_meta = []
        BATCH_SIZE = 50
        fts = ensure_fts(conn)
        # prefetch list in memory to compute freshness in batches
        rows = list(conn.execute("SELECT id, path, name, dir, ext FROM items"))
        for off in range(0, len(rows), BATCH_SIZE):
            chunk = rows[off: off + BATCH_SIZE]
            ids = [r["id"] for r in chunk]
            fresh = items_freshness(conn, ids)
            present = fts_ids_present(conn, ids) if fts else set()
            for row in chunk:
                processed += 1
                p = Path(row["path"]).resolve()
                if not any(str(p).startswith(r) for r in roots):
                    denied += 1
                    continue
                if not p.exists():
                    missing += 1
                    continue
                mtime, updated_at = fresh.get(row["id"], (0.0, None))
                needs_meta = force or (updated_at is None) or (updated_at < mtime)
                needs_fts = fts and (force or (row["id"] not in present) or needs_meta)
                if not (needs_meta or needs_fts):
                    skipped += 1
                    continue
                text = extract_text(p)
                tags, summary = heuristic_tags_and_summary(p, text)
                batch_meta.append((row["id"], tags, summary, __import__('time').time()))
                if fts:
                    batch.append((row["id"], row["name"], row["dir"], row["ext"], tags, summary, text or ""))
                if len(batch_meta) >= BATCH_SIZE:
                    upsert_items_meta(conn, batch_meta)
                    if fts and batch:
                        upsert_items_fts(conn, batch)
                        updated += len(batch)
                        batch.clear()
                    batch_meta.clear()
        if batch_meta:
            upsert_items_meta(conn, batch_meta)
        if fts and batch:
            upsert_items_fts(conn, batch)
            updated += len(batch)
        return self._json_response({
            "processed": processed,
            "updated": updated,
            "missing": missing,
            "denied": denied,
            "skipped": skipped,
            "fts": fts,
        })


def run_server(host: str = "127.0.0.1", port: int = 8080):
    # Ensure logger sinks are configured (stdout, optional file, memory)
    configure_logger()
    cfg = load_config()
    db = Path(cfg.database).resolve()
    conn = connect(db)
    init_db(conn)
    ensure_fts(conn)
    handler_cls = LibraryHandler
    handler_cls.cfg = cfg
    handler_cls.db_path = db
    handler_cls.scan_mgr = ScanManager()
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    logger.info("Serving on http://{}:{}", host, port)
    # Optionally kick off a background scan on startup
    if os.getenv("LIBINDEX_AUTOSCAN") in {"1", "true", "yes", "on"}:
        try:
            # Start scan in background immediately
            started = handler_cls.scan_mgr.start(cfg, db)
            if started:
                logger.info("Auto-scan started on server boot")
            else:
                logger.info("Auto-scan already running")
        except Exception as e:
            logger.exception("Failed to start auto-scan: {}", e)
    httpd.serve_forever()


class ScanManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._last_result: dict | None = None
        self._thread: threading.Thread | None = None

    def start(self, cfg: AppConfig, db_path: Path) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started_at = time.time()
            self._finished_at = None
            self._last_result = None

            def _runner():
                try:
                    logger.info("Background scan started")
                    conn = connect(db_path)

                    last_pct = {"v": -1}

                    def _progress(done: int, total: int):
                        with self._lock:
                            self._processed = done
                            self._total = total
                        if total:
                            pct = int(done * 100 / total)
                            if pct != last_pct["v"]:
                                last_pct["v"] = pct
                                logger.info("Scan progress: {}% ({} / {})", pct, done, total)

                    stats = scan_into(
                        conn, cfg.normalized_roots(), cfg.normalized_extensions(), progress_cb=_progress
                    )
                    result = {
                        "scanned": stats.scanned,
                        "updated": stats.added_or_updated,
                        "fts": stats.fts_updated,
                    }
                    logger.info("Background scan finished: {}", result)
                except Exception as e:
                    logger.exception("Background scan error: {}", e)
                    result = {"error": str(e)}
                finally:
                    with self._lock:
                        self._last_result = result
                        self._running = False
                        self._finished_at = time.time()

            self._thread = threading.Thread(target=_runner, name="scan-thread", daemon=True)
            self._thread.start()
            return True

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "result": self._last_result,
                "processed": getattr(self, "_processed", None),
                "total": getattr(self, "_total", None),
                "percent": (
                    int(getattr(self, "_processed", 0) * 100 / getattr(self, "_total", 1))
                    if getattr(self, "_total", None) else None
                ),
            }

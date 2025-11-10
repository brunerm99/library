from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile


MAX_TEXT_BYTES = 500_000  # limit stored content per file


def _truncate(s: str, max_bytes: int = MAX_TEXT_BYTES) -> str:
    b = s.encode("utf-8", errors="ignore")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def extract_text(path: Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext in {"txt"}:
            return _truncate(path.read_text(errors="ignore"))
        if ext in {"pdf"}:
            if shutil.which("pdftotext"):
                # Use popen to avoid temp files
                out = subprocess.check_output(["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"])
                return _truncate(out.decode("utf-8", errors="ignore"))
            return None
        if ext in {"epub"}:
            return _truncate(_extract_epub_text(path))
        # unsupported types: return None
        return None
    except Exception:
        return None


def _extract_epub_text(path: Path) -> str:
    parts: list[str] = []
    with ZipFile(path) as z:
        # heuristic: read up to first ~20 html/xhtml files
        html_names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
        for name in sorted(html_names)[:20]:
            try:
                data = z.read(name).decode("utf-8", errors="ignore")
            except Exception:
                continue
            text = _strip_html(data)
            if text:
                parts.append(text)
    return "\n".join(parts)


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = WS_RE.sub(" ", s)
    return s.strip()


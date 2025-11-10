from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

from .db import (
    upsert_items,
    ensure_fts,
    fetch_id_map_by_paths,
    upsert_items_fts,
    upsert_items_meta,
    items_freshness,
    fts_ids_present,
)
from .content import extract_text
from .ai import heuristic_tags_and_summary


@dataclass
class ScanStats:
    scanned: int = 0
    added_or_updated: int = 0
    skipped: int = 0
    fts_updated: int = 0


def iter_files(roots: Iterable[Path], extensions: Sequence[str]) -> Iterator[Path]:
    allowed = {e.lower().lstrip(".") for e in extensions}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # skip hidden dirs
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                ext = Path(fn).suffix.lower().lstrip(".")
                if ext in allowed:
                    yield Path(dirpath) / fn


def prepare_rows(paths: Iterable[Path]) -> List[Tuple]:
    now = time.time()
    rows = []
    for p in paths:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        name = p.name
        stem = p.stem
        ext = p.suffix.lower().lstrip(".")
        dir_ = str(p.parent)
        rows.append(
            (
                str(p.resolve()),
                dir_,
                name,
                stem,
                ext,
                int(st.st_size),
                float(st.st_mtime),
                name.lower(),
                dir_.lower(),
                now,
            )
        )
    return rows


def scan_into(conn, roots: Iterable[Path], extensions: Sequence[str], progress_cb=None) -> ScanStats:
    stats = ScanStats()
    files = list(iter_files(roots, extensions))
    total = len(files)
    stats.scanned = total
    if progress_cb:
        try:
            progress_cb(0, total)
        except Exception:
            pass
    if not files:
        return stats
    # Aim for ~5% increments per batch (≈20 batches), within sane bounds
    from math import ceil
    target_batches = 20
    BATCH = max(5, min(1000, ceil(total / target_batches)))
    processed = 0
    fts_ok = ensure_fts(conn)
    for i in range(0, total, BATCH):
        batch_paths = files[i : i + BATCH]
        rows = prepare_rows(batch_paths)
        if rows:
            upsert_items(conn, rows)
            stats.added_or_updated += len(rows)
            norm_paths = [str(Path(r[0]).resolve()) for r in rows]
            idmap = fetch_id_map_by_paths(conn, norm_paths)
            ids = [idmap[p] for p in norm_paths if p in idmap]
            fresh = items_freshness(conn, ids)
            present = fts_ids_present(conn, ids) if fts_ok else set()
            docs_fts = []
            docs_meta = []
            now = time.time()
            for p_str in norm_paths:
                item_id = idmap.get(p_str)
                if not item_id:
                    continue
                mtime, updated_at = fresh.get(item_id, (0.0, None))
                needs_meta = (updated_at is None) or (updated_at < mtime)
                needs_fts = fts_ok and (item_id not in present or needs_meta)
                if not (needs_meta or needs_fts):
                    continue
                p = Path(p_str)
                text = extract_text(p) if (needs_fts or needs_meta) else None
                tags, summary = heuristic_tags_and_summary(p, text)
                name = p.name
                dir_ = str(p.parent)
                ext = p.suffix.lower().lstrip(".")
                if needs_meta:
                    docs_meta.append((item_id, tags, summary, now))
                if needs_fts:
                    docs_fts.append((item_id, name, dir_, ext, tags, summary, text or ""))
            if docs_meta:
                upsert_items_meta(conn, docs_meta)
            if fts_ok and docs_fts:
                upsert_items_fts(conn, docs_fts)
                stats.fts_updated += len(docs_fts)
        processed += len(batch_paths)
        if progress_cb:
            try:
                progress_cb(min(processed, total), total)
            except Exception:
                pass
    return stats

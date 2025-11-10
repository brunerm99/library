from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, List, Dict, Set


SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  dir TEXT NOT NULL,
  name TEXT NOT NULL,
  stem TEXT NOT NULL,
  ext TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  name_lc TEXT NOT NULL,
  dir_lc TEXT NOT NULL,
  added_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_name_lc ON items(name_lc);
CREATE INDEX IF NOT EXISTS idx_items_ext ON items(ext);
CREATE INDEX IF NOT EXISTS idx_items_dir_lc ON items(dir_lc);

-- store tags/summary even if FTS is unavailable
CREATE TABLE IF NOT EXISTS items_meta (
  id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  tags TEXT,
  summary TEXT,
  updated_at REAL
);
-- starred items (favorites)
CREATE TABLE IF NOT EXISTS items_star (
  id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  starred_at REAL NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("schema", "1"))
    conn.commit()


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """Attempt to create FTS5 table. Returns True if available."""
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
              name, dir, ext, tags, summary, content,
              content_rowid='id'
            );
            """
        )
        # auxiliary indexes are internal in FTS; nothing else to do
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("fts", "1"))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("fts", "0"))
        conn.commit()
        return False


def upsert_items(conn: sqlite3.Connection, rows: Sequence[Tuple]) -> None:
    sql = (
        "INSERT INTO items(path, dir, name, stem, ext, size, mtime, name_lc, dir_lc, added_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "dir=excluded.dir, name=excluded.name, stem=excluded.stem, ext=excluded.ext, "
        "size=excluded.size, mtime=excluded.mtime, name_lc=excluded.name_lc, dir_lc=excluded.dir_lc"
    )
    with conn:
        conn.executemany(sql, rows)


def fetch_ids_by_paths(conn: sqlite3.Connection, paths: Sequence[Path | str]) -> List[int]:
    if not paths:
        return []
    qmarks = ",".join(["?"] * len(paths))
    cur = conn.execute(f"SELECT id FROM items WHERE path IN ({qmarks})", [str(Path(p).resolve()) for p in paths])
    return [r[0] for r in cur.fetchall()]


def fetch_id_map_by_paths(conn: sqlite3.Connection, paths: Sequence[Path | str]) -> Dict[str, int]:
    if not paths:
        return {}
    qmarks = ",".join(["?"] * len(paths))
    norm = [str(Path(p).resolve()) for p in paths]
    cur = conn.execute(f"SELECT id, path FROM items WHERE path IN ({qmarks})", norm)
    return {r[1]: r[0] for r in cur.fetchall()}


def items_freshness(conn: sqlite3.Connection, ids: Sequence[int]) -> Dict[int, Tuple[float, Optional[float]]]:
    """Return mapping id -> (mtime, updated_at or None)."""
    if not ids:
        return {}
    qmarks = ",".join(["?"] * len(ids))
    cur = conn.execute(
        f"""
        SELECT i.id, i.mtime, m.updated_at
        FROM items AS i
        LEFT JOIN items_meta AS m ON m.id = i.id
        WHERE i.id IN ({qmarks})
        """,
        list(ids),
    )
    return {r[0]: (float(r[1]), (float(r[2]) if r[2] is not None else None)) for r in cur.fetchall()}


def fts_ids_present(conn: sqlite3.Connection, ids: Sequence[int]) -> Set[int]:
    if not ids:
        return set()
    try:
        qmarks = ",".join(["?"] * len(ids))
        cur = conn.execute(f"SELECT rowid FROM items_fts WHERE rowid IN ({qmarks})", list(ids))
        return {int(r[0]) for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def upsert_items_fts(
    conn: sqlite3.Connection,
    docs: Sequence[Tuple[int, str, str, str, str, str, str]],
) -> None:
    """Insert or replace docs into items_fts.

    docs: list of tuples (id, name, dir, ext, tags, summary, content)
    """
    if not docs:
        return
    sql_del = "DELETE FROM items_fts WHERE rowid=?"
    sql_ins = "INSERT INTO items_fts(rowid, name, dir, ext, tags, summary, content) VALUES(?,?,?,?,?,?,?)"
    with conn:
        conn.executemany(sql_del, [(d[0],) for d in docs])
        conn.executemany(sql_ins, docs)


def upsert_items_meta(
    conn: sqlite3.Connection,
    docs: Sequence[Tuple[int, str, str, float]],
) -> None:
    """Insert or replace into items_meta.

    docs: (id, tags, summary, updated_at)
    """
    if not docs:
        return
    sql = (
        "INSERT INTO items_meta(id, tags, summary, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET tags=excluded.tags, summary=excluded.summary, updated_at=excluded.updated_at"
    )
    with conn:
        conn.executemany(sql, docs)


def delete_missing(conn: sqlite3.Connection, existing_paths: Iterable[Path]) -> int:
    # Remove items whose path no longer exists.
    existing = {str(Path(p).resolve()) for p in existing_paths}
    to_delete = []
    for row in conn.execute("SELECT path FROM items"):
        if row["path"] not in existing:
            to_delete.append((row["path"],))
    if not to_delete:
        return 0
    with conn:
        conn.executemany("DELETE FROM items WHERE path=?", to_delete)
    return len(to_delete)


def search(
    conn: sqlite3.Connection,
    q: Optional[str] = None,
    ext: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[int, list[sqlite3.Row]]:
    where = []
    args: list = []
    if q:
        where.append("name_lc LIKE ?")
        args.append(f"%{q.lower()}%")
    if ext:
        where.append("ext = ?")
        args.append(ext.lower().lstrip("."))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM items{where_sql}", args).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT items.*, CASE WHEN star.starred_at IS NULL THEN 0 ELSE 1 END AS starred,
               star.starred_at AS starred_at
        FROM items
        LEFT JOIN items_star AS star ON star.id = items.id
        {where_sql}
        ORDER BY (star.starred_at IS NOT NULL) DESC, items.name_lc
        LIMIT ? OFFSET ?
        """,
        (*args, int(limit), int(offset)),
    ).fetchall()
    return total, rows


def smart_search(
    conn: sqlite3.Connection,
    q: Optional[str],
    ext: Optional[str],
    limit: int = 50,
    offset: int = 0,
) -> Tuple[int, list[sqlite3.Row]]:
    if not q:
        # fall back to normal search if no query
        return search(conn, q=None, ext=ext, limit=limit, offset=offset)
    import re

    # check if FTS available
    fts_val = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
    fts_ok = fts_val and fts_val[0] == "1"
    tokens = re.findall(r"[\w\-]+", q.lower())
    if not tokens:
        return 0, []

    if fts_ok:
        fts_query = " ".join(f"{t}*" for t in tokens)
        where = "items_fts MATCH ?"
        args: list = [fts_query]
        if ext:
            where += " AND items.ext = ?"
            args.append(ext.lower().lstrip("."))
        total = conn.execute(
            f"SELECT COUNT(*) FROM items_fts JOIN items ON items_fts.rowid = items.id WHERE {where}",
            args,
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT items.*, snippet(items_fts, 5, '[', ']', '…', 10) AS snippet,
                   bm25(items_fts) AS rank,
                   CASE WHEN star.starred_at IS NULL THEN 0 ELSE 1 END AS starred,
                   star.starred_at AS starred_at
            FROM items_fts
            JOIN items ON items_fts.rowid = items.id
            LEFT JOIN items_star AS star ON star.id = items.id
            WHERE {where}
            ORDER BY (star.starred_at IS NOT NULL) DESC, rank
            LIMIT ? OFFSET ?
            """.format(where=where),
            (*args, int(limit), int(offset)),
        ).fetchall()
        return total, rows

    # fallback: LIKE over name + tags + summary (quick; no content scan)
    like = f"%{q.lower()}%"
    args: list = [like, like]
    where = "(items.name_lc LIKE ? OR COALESCE(items_meta.tags,'') LIKE ?)"
    if ext:
        where += " AND items.ext = ?"
        args.append(ext.lower().lstrip("."))
    total = conn.execute(
        f"SELECT COUNT(*) FROM items LEFT JOIN items_meta ON items.id = items_meta.id WHERE {where}",
        args,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT items.*, CASE WHEN star.starred_at IS NULL THEN 0 ELSE 1 END AS starred,
               star.starred_at AS starred_at
        FROM items
        LEFT JOIN items_meta ON items.id = items_meta.id
        LEFT JOIN items_star AS star ON star.id = items.id
        WHERE {where}
        ORDER BY (star.starred_at IS NOT NULL) DESC, items.name_lc
        LIMIT ? OFFSET ?
        """,
        (*args, int(limit), int(offset)),
    ).fetchall()
    return total, rows


def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def set_star(conn: sqlite3.Connection, item_id: int, on: bool) -> None:
    if on:
        conn.execute(
            "INSERT OR REPLACE INTO items_star(id, starred_at) VALUES(?, ?)", (item_id, time.time())
        )
        conn.commit()
    else:
        conn.execute("DELETE FROM items_star WHERE id=?", (item_id,))
        conn.commit()


def is_starred(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM items_star WHERE id=?", (item_id,)).fetchone()
    return bool(row)

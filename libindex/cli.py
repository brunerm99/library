from __future__ import annotations

import argparse
import os
from pathlib import Path

from .logutil import logger
from .config import load_config, save_config
from .db import connect, init_db
from .scanner import scan_into
from .server import run_server


def _env_roots() -> list[str] | None:
    env = os.getenv("LIBRARY_ROOTS")
    if not env:
        return None
    sep = ";" if os.name == "nt" else ":"
    return [p for p in env.split(sep) if p]


def cmd_init(args):
    cfg = load_config()  # creates default if missing
    conn = connect(cfg.database)
    init_db(conn)
    logger.info("Initialized database at {}", Path(cfg.database).resolve())
    logger.info("Config at {}", Path('config.json').resolve())


def cmd_scan(args):
    cfg = load_config()
    roots = args.roots or _env_roots()
    if roots:
        cfg.roots = [str(Path(r).expanduser().resolve()) for r in roots]
        save_config(cfg)
    conn = connect(cfg.database)
    init_db(conn)
    last = {"pct": -1}

    def _progress(done, total):
        if not total:
            return
        pct = int(done * 100 / total)
        if pct != last["pct"]:
            last["pct"] = pct
            logger.info("Scan progress: {}% ({} / {})", pct, done, total)

    stats = scan_into(conn, cfg.normalized_roots(), cfg.normalized_extensions(), progress_cb=_progress)
    logger.info(
        "Scanned: {}, Updated: {}, FTS: {}",
        stats.scanned,
        stats.added_or_updated,
        stats.fts_updated,
    )


def cmd_serve(args):
    run_server(host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(description="Library indexer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize DB and default config")
    p_init.set_defaults(func=cmd_init)

    p_scan = sub.add_parser("scan", help="Scan directories into the database")
    p_scan.add_argument("--roots", nargs="*", help="One or more directories to scan")
    p_scan.set_defaults(func=cmd_scan)

    p_serve = sub.add_parser("serve", help="Run web server and UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

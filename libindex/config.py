from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List


DEFAULT_EXTENSIONS = [
    "pdf",
    "epub",
    "mobi",
    "azw3",
    "djvu",
    "cbz",
    "cbr",
    "txt",
]


@dataclass
class AppConfig:
    database: str = "library.db"
    roots: List[str] = None
    extensions: List[str] = None

    def normalized_extensions(self) -> List[str]:
        exts = self.extensions or DEFAULT_EXTENSIONS
        return sorted({e.lower().lstrip(".") for e in exts})

    def normalized_roots(self) -> List[Path]:
        if self.roots:
            return [Path(r).expanduser().resolve() for r in self.roots]
        # default to current directory
        return [Path.cwd().resolve()]


def config_path() -> Path:
    return Path("config.json").resolve()


def load_config() -> AppConfig:
    p = config_path()
    if not p.exists():
        # create default
        cfg = AppConfig(roots=[str(Path.cwd())], extensions=DEFAULT_EXTENSIONS)
        save_config(cfg)
        return cfg
    data = json.loads(p.read_text())
    return AppConfig(
        database=data.get("database", "library.db"),
        roots=data.get("roots") or [str(Path.cwd())],
        extensions=data.get("extensions") or DEFAULT_EXTENSIONS,
    )


def save_config(cfg: AppConfig) -> None:
    p = config_path()
    data = asdict(cfg)
    # ensure relative, but keep as provided
    p.write_text(json.dumps(data, indent=2))


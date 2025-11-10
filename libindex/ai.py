from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Tuple


STOPWORDS = {
    "the","a","an","of","and","to","in","on","for","with","by","at","from","about","as","into","like","through","after","over","between","out","against","during","without","before","under","around","among",
    "vol","edition","ed","v","ver","rev","revised","part","pt","no","nr","copy","draft","final","new","second","third","fourth","fifth",
}


def heuristic_tags_and_summary(path: Path, content: str | None) -> Tuple[str, str]:
    """Generate simple tags and summary from filename/dir/content."""
    name = path.stem
    dir_name = path.parent.name
    base_tokens = _tokenize(name) + _tokenize(dir_name)
    content_tokens = _tokenize(content or "")[:500]
    tokens = [t for t in base_tokens + content_tokens if t not in STOPWORDS and not t.isdigit() and len(t) > 2]
    # term weights favor filename tokens
    weights = Counter(tokens)
    common = [w for w,_ in weights.most_common(12)]
    tags = ", ".join(dict.fromkeys(common))
    # summary: first sentence from content or constructed from tags
    summary = ""
    if content:
        summary = _first_sentence(content)
    if not summary:
        summary = f"Topics: {tags}" if tags else f"File: {path.name}"
    return tags, summary


def _tokenize(s: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9\-]+", s.lower())


def _first_sentence(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    # crude sentence split
    m = re.search(r"([\s\S]{0,400}?[\.!?])\s", s)
    if m:
        return m.group(1).strip()
    return s[:200].strip()


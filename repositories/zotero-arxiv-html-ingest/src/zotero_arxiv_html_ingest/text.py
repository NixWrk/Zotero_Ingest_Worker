from __future__ import annotations

import difflib
import html
import re


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def strip_html(value: str) -> str:
    return normalize_space(re.sub(r"<[^>]+>", " ", str(value or "")))


def normalize_title(value: str) -> str:
    value = normalize_space(value).casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_space(value)


def title_match_score(left: str, right: str) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)
    return round((ratio * 0.65) + (overlap * 0.35), 4)


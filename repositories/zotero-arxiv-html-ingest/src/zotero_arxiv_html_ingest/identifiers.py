from __future__ import annotations

import html
import re


def extract_arxiv_id_from_text(text: str) -> str | None:
    patterns = [
        r"(?i)\b10\.48550/arxiv\.([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
        r"(?i)\barxiv\s*:\s*([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
        r"(?i)\barxiv\.org/(?:abs|pdf|html)/([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_arxiv_id("".join(part or "" for part in match.groups()))
    return None


def normalize_arxiv_id(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"(?i)^arxiv:\s*", "", value)
    value = re.sub(r"(?i)^https?://arxiv\.org/(?:abs|pdf|html)/", "", value)
    value = re.sub(r"(?i)^10\.48550/arxiv\.", "", value)
    value = value.strip().rstrip(".,;:)]}")
    value = re.sub(r"(?i)\.pdf$", "", value)
    return re.sub(r"(?i)v\d+$", "", value)


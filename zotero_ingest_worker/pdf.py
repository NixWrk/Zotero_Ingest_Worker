from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass(frozen=True)
class PdfTextInfo:
    has_text: bool
    char_count: int
    pages_checked: int
    pages_total: int
    image_count: int = 0
    xobject_count: int = 0
    content_stream_bytes: int = 0
    error: str | None = None

    @property
    def is_blank_or_empty(self) -> bool:
        return (
            self.error is None
            and not self.has_text
            and self.char_count == 0
            and self.image_count == 0
            and self.content_stream_bytes == 0
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["is_blank_or_empty"] = self.is_blank_or_empty
        return payload


@dataclass(frozen=True)
class PdfProcessingInfo:
    pages_total: int
    is_encrypted: bool
    decrypted_with_empty_password: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PdfLanguageInfo:
    detected_language: str
    confidence: float
    reason: str
    text_chars: int
    word_count: int
    latin_chars: int
    cyrillic_chars: int
    latin_ratio: float
    cyrillic_ratio: float
    english_stopword_hits: int
    russian_stopword_hits: int
    pages_total: int
    pages_sampled: tuple[int, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_pdf_processing(path: Path) -> PdfProcessingInfo:
    try:
        reader = PdfReader(str(path))
        is_encrypted = bool(getattr(reader, "is_encrypted", False))
        decrypted = False
        if is_encrypted:
            try:
                decrypted = bool(reader.decrypt(""))
            except Exception:
                decrypted = False
        pages_total = len(reader.pages)
        return PdfProcessingInfo(
            pages_total=pages_total,
            is_encrypted=is_encrypted,
            decrypted_with_empty_password=decrypted,
        )
    except Exception as exc:
        return PdfProcessingInfo(
            pages_total=0,
            is_encrypted=False,
            decrypted_with_empty_password=False,
            error=str(exc),
        )


def detect_pdf_language(path: Path, *, max_pages: int = 15) -> PdfLanguageInfo:
    try:
        reader = PdfReader(str(path))
        pages_total = len(reader.pages)
        page_indexes = _sample_pdf_page_indexes(pages_total, max_pages=max_pages)
        text = "\n".join(reader.pages[index].extract_text() or "" for index in page_indexes)
        return detect_text_language(
            text,
            pages_total=pages_total,
            pages_sampled=tuple(index + 1 for index in page_indexes),
        )
    except Exception as exc:
        return PdfLanguageInfo(
            detected_language="unknown",
            confidence=0.0,
            reason="pdf_language_detection_error",
            text_chars=0,
            word_count=0,
            latin_chars=0,
            cyrillic_chars=0,
            latin_ratio=0.0,
            cyrillic_ratio=0.0,
            english_stopword_hits=0,
            russian_stopword_hits=0,
            pages_total=0,
            pages_sampled=(),
            error=str(exc),
        )


def detect_text_language(
    text: str,
    *,
    pages_total: int = 0,
    pages_sampled: tuple[int, ...] = (),
) -> PdfLanguageInfo:
    normalized = re.sub(r"\s+", " ", text).strip()
    words = [word.lower() for word in _WORD_RE.findall(normalized)]
    latin_chars = len(_LATIN_RE.findall(normalized))
    cyrillic_chars = len(_CYRILLIC_RE.findall(normalized))
    alpha_total = max(latin_chars + cyrillic_chars, 1)
    latin_ratio = latin_chars / alpha_total
    cyrillic_ratio = cyrillic_chars / alpha_total
    counts = Counter(words)
    english_hits = sum(counts[word] for word in _EN_STOPWORDS)
    russian_hits = sum(counts[word] for word in _RU_STOPWORDS)

    detected = "unknown"
    confidence = 0.0
    reason = "insufficient_text"
    if latin_chars + cyrillic_chars >= 120 and len(words) >= 20:
        if cyrillic_ratio >= 0.35 or (
            cyrillic_chars >= 80 and russian_hits >= max(8, english_hits * 2)
        ):
            detected = "ru"
            confidence = max(0.82, min(0.99, cyrillic_ratio + min(russian_hits / 120, 0.25)))
            reason = "cyrillic_majority_russian_stopwords"
        elif latin_ratio >= 0.70 and english_hits >= max(8, russian_hits * 2):
            detected = "en"
            confidence = max(0.82, min(0.99, latin_ratio + min(english_hits / 180, 0.20)))
            reason = "latin_majority_english_stopwords"
        elif latin_ratio >= 0.85 and cyrillic_ratio <= 0.03:
            detected = "en"
            confidence = 0.78
            reason = "latin_majority_low_stopwords"
        elif latin_ratio >= 0.20 and cyrillic_ratio >= 0.20:
            detected = "mixed"
            confidence = max(latin_ratio, cyrillic_ratio)
            reason = "mixed_latin_cyrillic"
        else:
            reason = "low_language_confidence"

    return PdfLanguageInfo(
        detected_language=detected,
        confidence=round(float(confidence), 3),
        reason=reason,
        text_chars=len(normalized),
        word_count=len(words),
        latin_chars=latin_chars,
        cyrillic_chars=cyrillic_chars,
        latin_ratio=round(float(latin_ratio), 3),
        cyrillic_ratio=round(float(cyrillic_ratio), 3),
        english_stopword_hits=english_hits,
        russian_stopword_hits=russian_hits,
        pages_total=pages_total,
        pages_sampled=pages_sampled,
    )


def inspect_text_layer(path: Path, *, min_chars: int = 80, max_pages: int = 5) -> PdfTextInfo:
    try:
        reader = PdfReader(str(path))
        pages_total = len(reader.pages)
        page_indexes = _sample_text_layer_page_indexes(pages_total, max_pages=max_pages)
        char_count = 0
        image_count = 0
        xobject_count = 0
        content_stream_bytes = 0
        for pages_checked, index in enumerate(page_indexes, start=1):
            page = reader.pages[index]
            page_image_count, page_xobject_count = _page_xobject_counts(page)
            image_count += page_image_count
            xobject_count += page_xobject_count
            content_stream_bytes += _content_stream_size(page)
            text = page.extract_text() or ""
            char_count += len(text.strip())
            if char_count >= min_chars:
                return PdfTextInfo(
                    has_text=True,
                    char_count=char_count,
                    pages_checked=pages_checked,
                    pages_total=pages_total,
                    image_count=image_count,
                    xobject_count=xobject_count,
                    content_stream_bytes=content_stream_bytes,
                )
        return PdfTextInfo(
            has_text=False,
            char_count=char_count,
            pages_checked=len(page_indexes),
            pages_total=pages_total,
            image_count=image_count,
            xobject_count=xobject_count,
            content_stream_bytes=content_stream_bytes,
        )
    except Exception as exc:
        return PdfTextInfo(
            has_text=False,
            char_count=0,
            pages_checked=0,
            pages_total=0,
            error=str(exc),
        )


def _sample_text_layer_page_indexes(page_count: int, *, max_pages: int) -> list[int]:
    if page_count <= 0 or max_pages <= 0:
        return []
    limit = min(page_count, max(1, max_pages))
    if page_count <= limit:
        return list(range(page_count))

    positions: list[int] = []

    def add(index: int) -> None:
        index = max(0, min(page_count - 1, index))
        if index not in positions and len(positions) < limit:
            positions.append(index)

    add(0)
    if limit >= 4:
        add(1)
    if limit >= 5:
        add(2)
    if limit >= 3:
        add(page_count // 2)
    if limit >= 2:
        add(page_count - 1)

    candidate = 1
    while len(positions) < limit:
        add(round((page_count - 1) * candidate / (limit + 1)))
        candidate += 1

    return sorted(positions)


def _sample_pdf_page_indexes(page_count: int, *, max_pages: int) -> list[int]:
    if page_count <= 0 or max_pages <= 0:
        return []
    if page_count <= max_pages:
        return list(range(page_count))
    positions = {0, 1, 2, page_count - 3, page_count - 2, page_count - 1}
    remaining = max_pages - len([pos for pos in positions if 0 <= pos < page_count])
    if remaining > 0:
        span = page_count - 1
        for index in range(remaining):
            positions.add(round(span * (index + 1) / (remaining + 1)))
    return sorted(pos for pos in positions if 0 <= pos < page_count)[:max_pages]


def _content_stream_size(page: object) -> int:
    contents = page.get_contents()
    if contents is None:
        return 0
    if isinstance(contents, list):
        return sum(_stream_data_size(stream) for stream in contents)
    return _stream_data_size(contents)


def _stream_data_size(stream: object) -> int:
    stream = _dereference(stream)
    if hasattr(stream, "get_data"):
        data = stream.get_data()
        return len(data or b"")
    return 0


def _page_xobject_counts(page: object) -> tuple[int, int]:
    resources = _dereference(page.get("/Resources") or {})
    if not hasattr(resources, "get"):
        return 0, 0
    xobjects = _dereference(resources.get("/XObject"))
    if not xobjects or not hasattr(xobjects, "items"):
        return 0, 0

    image_count = 0
    xobject_count = 0
    for _name, raw_xobject in xobjects.items():
        xobject_count += 1
        xobject = _dereference(raw_xobject)
        if hasattr(xobject, "get") and str(xobject.get("/Subtype")) == "/Image":
            image_count += 1
    return image_count, xobject_count


def _dereference(value: object) -> object:
    if hasattr(value, "get_object"):
        return value.get_object()
    return value


def assert_valid_searchable_pdf(path: Path, *, min_chars: int, max_pages: int) -> PdfTextInfo:
    if not path.exists():
        raise FileNotFoundError(f"OCR output does not exist: {path}")
    if path.stat().st_size <= 0:
        raise ValueError(f"OCR output is empty: {path}")
    info = inspect_text_layer(path, min_chars=min_chars, max_pages=max_pages)
    if info.error:
        raise ValueError(f"OCR output is not a readable PDF: {info.error}")
    if not info.has_text:
        raise ValueError(
            "OCR output still does not appear to contain a text layer "
            f"(chars={info.char_count}, pages_checked={info.pages_checked})."
        )
    return info


_WORD_RE = re.compile(r"[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u00FF\u0400-\u04FF]+")
_LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u00FF]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_EN_STOPWORDS = {
    "a",
    "about",
    "after",
    "also",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "between",
    "by",
    "can",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "may",
    "not",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "these",
    "this",
    "to",
    "using",
    "was",
    "were",
    "which",
    "with",
}
_RU_STOPWORDS = {
    "без",
    "более",
    "был",
    "была",
    "были",
    "быть",
    "в",
    "во",
    "для",
    "до",
    "его",
    "ее",
    "если",
    "из",
    "или",
    "к",
    "как",
    "между",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "после",
    "при",
    "с",
    "со",
    "так",
    "также",
    "то",
    "у",
    "что",
    "это",
}

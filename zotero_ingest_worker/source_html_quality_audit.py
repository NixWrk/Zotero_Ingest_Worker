from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from zoteropdf2md.html_links import (
    count_same_document_absolute_fragment_links,
    extract_html_fragment_targets,
    is_plain_local_fragment,
    urlsplit_or_none,
)

from .config import discover_zotero_data_dirs, from_env, is_zotero_data_dir, unique_paths
from .local_zotero_paths import library_id_for_data_dir


SOURCE_HTML_RE = re.compile(r"\[\s*source\s+html\s*\]|\bsource\s+html\b", re.IGNORECASE)
ARXIV_HTML_RE = re.compile(r"\[\s*arxiv\s+html\s*\]|\barxiv\s+html\b", re.IGNORECASE)
GENERATED_HTML_RE = re.compile(r"\[(?:[a-z]{2,12}|mixed|unknown) html\]\.html?$", re.IGNORECASE)
LANGUAGE_HTML_RE = re.compile(r"\[([a-z0-9]{2,12}|mixed|unknown) html\](?:\.x?html?)?", re.IGNORECASE)
SPRINGER_TABLE_PLACEHOLDER_RE = re.compile(r"/tables/\d+\b", re.IGNORECASE)
IGNORED_BLOCK_RE = re.compile(r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>", re.IGNORECASE)
DEF_LIST_RE = re.compile(r"<dl\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bdef-list\b)", re.IGNORECASE)
LTX_TABLE_RE = re.compile(r"<figure\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bltx_table\b)", re.IGNORECASE)
DISP_FORMULA_TABLE_RE = re.compile(r"<table\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bdisp-formula\b)", re.IGNORECASE)
DISPLAY_MATH_RE = re.compile(r"<math\b(?=[^>]*\bdisplay\s*=\s*['\"]block['\"])", re.IGNORECASE)
LTX_ROWCOLOR_RE = re.compile(
    r"(?:\\rowcolor|<span\b(?=[^>]*\bltx_ERROR\b)[^>]*>\s*\\rowcolor\s*</span>)",
    re.IGNORECASE,
)
LTX_ITEMIZE_MARKER_BLOCK_RE = re.compile(
    r"<li\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bltx_item\b)[^>]*>\s*"
    r"<span\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bltx_tag_item\b)[^>]*>[\s\S]{0,120}</span>\s*"
    r"<div\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bltx_para\b)",
    re.IGNORECASE,
)
LTX_INLINE_BLACK_TEXT_RE = re.compile(
    r"<span\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bltx_text\b)"
    r"(?=[^>]*\bstyle\s*=\s*['\"][^'\"]*\bcolor\s*:\s*(?:#000(?:000)?|black)\b)",
    re.IGNORECASE,
)
LTX_BLACK_MATHCOLOR_RE = re.compile(
    r"\bmathcolor\s*=\s*['\"]\s*(?:#000(?:000)?|black)\s*['\"]",
    re.IGNORECASE,
)
LOCAL_HTML_SUFFIXES = {".html", ".htm"}
SUPPLEMENTARY_RESOURCE_SUFFIXES = {
    ".bib",
    ".csv",
    ".doc",
    ".docx",
    ".m4v",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".pdf",
    ".ppt",
    ".pptx",
    ".qt",
    ".txt",
    ".webm",
    ".xls",
    ".xlsx",
    ".zip",
}

CRITICAL_ISSUES = {
    "missing_zotero_attachment_record",
    "missing_web_polish_style",
    "missing_web_doc_main",
    "missing_source_kind",
    "unresolved_local_fragment_links",
    "absolute_fragment_links_resolve_local",
    "image_missing_src",
    "image_relative_missing_file",
    "image_data_non_image_mime",
    "image_bad_data_url",
    "picture_source_overrides_inline_image",
    "latexml_figure_render_error",
    "latexml_rowcolor_artifact",
    "frontiers_reference_button",
    "frontiers_empty_article_reference_link",
    "frontiers_figure_js_control",
    "nested_web_doc_wrapper",
    "pmc_dead_ui_control",
    "missing_def_list_style",
    "missing_latexml_caption_style",
    "missing_latexml_table_style",
    "missing_formula_style",
    "stale_arxiv_html_attachment",
    "script_tags_present",
    "table_without_rows",
    "springer_table_placeholder",
    "html_attachment_missing_file",
    "latexml_itemize_marker_layout",
    "latexml_inline_black_text",
    "latexml_math_black_color",
}

WARNING_ISSUES = {
    "figure_without_media_warning",
    "no_succeeded_source_html_job",
}


@dataclass(frozen=True)
class HtmlAttachmentRow:
    key: str
    parent_key: str | None
    title: str
    zotero_path: str
    file_path: Path


@dataclass(frozen=True)
class SourceHtmlJobIndex:
    enabled: bool
    counts: dict[str, int]
    succeeded_source_keys: set[tuple[str, str]]
    succeeded_source_attachment_keys: set[str]
    db_errors: list[dict[str, str]]


@dataclass
class HtmlMetrics:
    links: int = 0
    local_fragment_unresolved: int = 0
    absolute_fragment_links: int = 0
    absolute_fragment_links_resolve_local: int = 0
    images: int = 0
    image_missing_src: int = 0
    image_remote_src: int = 0
    image_relative_src: int = 0
    image_relative_missing_file: int = 0
    image_data_non_image_mime: int = 0
    image_bad_data_url: int = 0
    image_empty_alt: int = 0
    picture_inline_data_img_with_source: int = 0
    figures: int = 0
    figure_without_media_warning: int = 0
    latexml_figure_render_error: int = 0
    latexml_rowcolor_artifacts: int = 0
    frontiers_reference_buttons: int = 0
    frontiers_empty_article_reference_links: int = 0
    frontiers_figure_js_controls: int = 0
    pmc_dead_ui_controls: int = 0
    web_doc_mains: int = 0
    tables: int = 0
    table_rows: int = 0
    table_cells: int = 0
    table_without_rows: int = 0
    def_lists: int = 0
    latexml_caption_transformed_blocks: int = 0
    latexml_tables: int = 0
    latexml_itemize_marker_blocks: int = 0
    latexml_inline_black_text_styles: int = 0
    latexml_black_mathcolor_attrs: int = 0
    disp_formula_tables: int = 0
    display_math_blocks: int = 0
    scripts: int = 0
    springer_table_placeholder_markers: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "links": self.links,
            "local_fragment_unresolved": self.local_fragment_unresolved,
            "absolute_fragment_links": self.absolute_fragment_links,
            "absolute_fragment_links_resolve_local": self.absolute_fragment_links_resolve_local,
            "images": self.images,
            "image_missing_src": self.image_missing_src,
            "image_remote_src": self.image_remote_src,
            "image_relative_src": self.image_relative_src,
            "image_relative_missing_file": self.image_relative_missing_file,
            "image_data_non_image_mime": self.image_data_non_image_mime,
            "image_bad_data_url": self.image_bad_data_url,
            "image_empty_alt": self.image_empty_alt,
            "picture_inline_data_img_with_source": self.picture_inline_data_img_with_source,
            "figures": self.figures,
            "figure_without_media_warning": self.figure_without_media_warning,
            "latexml_figure_render_error": self.latexml_figure_render_error,
            "latexml_rowcolor_artifacts": self.latexml_rowcolor_artifacts,
            "frontiers_reference_buttons": self.frontiers_reference_buttons,
            "frontiers_empty_article_reference_links": self.frontiers_empty_article_reference_links,
            "frontiers_figure_js_controls": self.frontiers_figure_js_controls,
            "pmc_dead_ui_controls": self.pmc_dead_ui_controls,
            "web_doc_mains": self.web_doc_mains,
            "tables": self.tables,
            "table_rows": self.table_rows,
            "table_cells": self.table_cells,
            "table_without_rows": self.table_without_rows,
            "def_lists": self.def_lists,
            "latexml_caption_transformed_blocks": self.latexml_caption_transformed_blocks,
            "latexml_tables": self.latexml_tables,
            "latexml_itemize_marker_blocks": self.latexml_itemize_marker_blocks,
            "latexml_inline_black_text_styles": self.latexml_inline_black_text_styles,
            "latexml_black_mathcolor_attrs": self.latexml_black_mathcolor_attrs,
            "disp_formula_tables": self.disp_formula_tables,
            "display_math_blocks": self.display_math_blocks,
            "scripts": self.scripts,
            "springer_table_placeholder_markers": self.springer_table_placeholder_markers,
        }


class ArticleHtmlAuditParser(HTMLParser):
    def __init__(self, *, base_dir: Path):
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.metrics = HtmlMetrics()
        self.hrefs: list[str] = []
        self.fragment_targets: set[str] = set()
        self.remote_image_hosts: Counter[str] = Counter()
        self.relative_missing_images: list[str] = []
        self.unresolved_local_fragments: list[str] = []
        self.web_doc_main = False
        self.style_ok = False
        self.source_kind = ""
        self._text_parts: list[str] = []
        self._ignored_depth = 0
        self._figure_stack: list[bool] = []
        self._picture_stack: list[dict[str, bool]] = []
        self._table_stack: list[dict[str, int]] = []

    @property
    def text_length(self) -> int:
        return len(" ".join(part.strip() for part in self._text_parts if part.strip()))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        self._record_fragment_target(attr_map)

        if tag_name in {"script", "style"}:
            self._ignored_depth += 1
        if tag_name == "script":
            self.metrics.scripts += 1
        if tag_name == "style" and attr_map.get("data-z2m-style") == "web-html-polish":
            self.style_ok = True
        if tag_name == "main" and attr_map.get("id") == "web-doc":
            self.metrics.web_doc_mains += 1
            self.web_doc_main = True
            self.source_kind = attr_map.get("data-z2m-source-kind", "").strip()
        class_tokens = attr_map.get("class", "").split()
        if tag_name == "button" and "ArticleReference" in class_tokens:
            self.metrics.frontiers_reference_buttons += 1
        if tag_name == "a" and "ArticleReference" in class_tokens:
            href = unescape(attr_map.get("href", "")).strip()
            data_event = attr_map.get("data-event", "")
            if not href and data_event.casefold().startswith("articlereference-a-"):
                self.metrics.frontiers_empty_article_reference_links += 1
        if _is_frontiers_figure_js_control(tag_name, attr_map):
            self.metrics.frontiers_figure_js_controls += 1
        if _is_pmc_dead_ui_control(tag_name, attr_map):
            self.metrics.pmc_dead_ui_controls += 1

        if tag_name == "a" and "href" in attr_map:
            href = unescape(attr_map.get("href", "")).strip()
            if href:
                self.hrefs.append(href)
                self.metrics.links += 1
                parsed = urlsplit_or_none(href)
                if parsed is not None and parsed.scheme in {"http", "https"} and parsed.fragment:
                    self.metrics.absolute_fragment_links += 1
                if _looks_like_supplementary_resource_href(href):
                    self._mark_current_figure_has_media()

        if tag_name == "figure":
            self.metrics.figures += 1
            self._figure_stack.append(_figure_tag_has_intrinsic_content(attr_map))
        elif self._figure_stack and "ltx_error" in attr_map.get("class", "").casefold():
            self.metrics.latexml_figure_render_error += 1
        elif tag_name in {"audio", "canvas", "embed", "iframe", "img", "math", "object", "picture", "svg", "video"}:
            self._mark_current_figure_has_media()

        if tag_name == "picture":
            self._picture_stack.append({"source": False, "inline_img": False})
        elif tag_name == "source" and self._picture_stack:
            self._picture_stack[-1]["source"] = True

        if tag_name == "img":
            self._record_image(attr_map)

        if tag_name == "table":
            self.metrics.tables += 1
            self._mark_current_figure_has_media()
            self._table_stack.append({"rows": 0, "cells": 0})
        elif tag_name == "tr":
            self.metrics.table_rows += 1
            if self._table_stack:
                self._table_stack[-1]["rows"] += 1
        elif tag_name in {"td", "th"}:
            self.metrics.table_cells += 1
            if self._table_stack:
                self._table_stack[-1]["cells"] += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style"} and self._ignored_depth > 0:
            self._ignored_depth -= 1
        if tag_name == "figure" and self._figure_stack:
            has_media = self._figure_stack.pop()
            if not has_media:
                self.metrics.figure_without_media_warning += 1
        if tag_name == "picture" and self._picture_stack:
            picture = self._picture_stack.pop()
            if picture["source"] and picture["inline_img"]:
                self.metrics.picture_inline_data_img_with_source += 1
        if tag_name == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table["rows"] == 0:
                self.metrics.table_without_rows += 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data.strip():
            self._text_parts.append(data)

    def finalize(self, html: str) -> None:
        content_html = _html_without_ignored_blocks(html)
        targets = self.fragment_targets or extract_html_fragment_targets(html)
        for href in self.hrefs:
            parsed = urlsplit_or_none(href)
            if parsed is None or not is_plain_local_fragment(parsed):
                continue
            target = urllib.parse.unquote(parsed.fragment)
            if target not in targets:
                self.metrics.local_fragment_unresolved += 1
                if len(self.unresolved_local_fragments) < 20:
                    self.unresolved_local_fragments.append(href)
        self.metrics.absolute_fragment_links_resolve_local = count_same_document_absolute_fragment_links(html)
        self.metrics.springer_table_placeholder_markers = len(SPRINGER_TABLE_PLACEHOLDER_RE.findall(content_html))
        self.metrics.latexml_rowcolor_artifacts = len(LTX_ROWCOLOR_RE.findall(content_html))
        self.metrics.def_lists = len(DEF_LIST_RE.findall(content_html))
        self.metrics.latexml_tables = len(LTX_TABLE_RE.findall(content_html))
        self.metrics.latexml_itemize_marker_blocks = len(LTX_ITEMIZE_MARKER_BLOCK_RE.findall(content_html))
        self.metrics.latexml_inline_black_text_styles = len(LTX_INLINE_BLACK_TEXT_RE.findall(content_html))
        self.metrics.latexml_black_mathcolor_attrs = len(LTX_BLACK_MATHCOLOR_RE.findall(content_html))
        self.metrics.disp_formula_tables = len(DISP_FORMULA_TABLE_RE.findall(content_html))
        self.metrics.display_math_blocks = len(DISPLAY_MATH_RE.findall(content_html))
        if "ltx_caption" in content_html and "ltx_transformed_outer" in content_html:
            self.metrics.latexml_caption_transformed_blocks = 1

    def _record_fragment_target(self, attr_map: dict[str, str]) -> None:
        for attr_name in ("id", "name"):
            value = unescape(attr_map.get(attr_name, "")).strip()
            if value:
                self.fragment_targets.add(value)

    def _record_image(self, attr_map: dict[str, str]) -> None:
        self.metrics.images += 1
        self._mark_current_figure_has_media()
        src = unescape(attr_map.get("src", "")).strip()
        alt = unescape(attr_map.get("alt", "")).strip()
        if not alt:
            self.metrics.image_empty_alt += 1
        if not src:
            self.metrics.image_missing_src += 1
            return
        if _is_data_url(src):
            if src.casefold().startswith("data:image/") and self._picture_stack:
                self._picture_stack[-1]["inline_img"] = True
            self._record_data_url(src)
            return
        parsed = urlsplit_or_none(src)
        if parsed is not None and parsed.scheme in {"http", "https"}:
            self.metrics.image_remote_src += 1
            if parsed.netloc:
                self.remote_image_hosts[parsed.netloc.lower()] += 1
            return
        if src.startswith("//"):
            self.metrics.image_remote_src += 1
            host = urllib.parse.urlsplit(f"https:{src}").netloc.lower()
            if host:
                self.remote_image_hosts[host] += 1
            return
        if parsed is not None and parsed.scheme:
            return

        self.metrics.image_relative_src += 1
        local_path = _resolve_relative_asset(src, base_dir=self.base_dir)
        if local_path is None or not local_path.is_file():
            self.metrics.image_relative_missing_file += 1
            if len(self.relative_missing_images) < 20:
                self.relative_missing_images.append(src)

    def _record_data_url(self, src: str) -> None:
        header, sep, data = src.partition(",")
        if not sep:
            self.metrics.image_bad_data_url += 1
            return
        mime = header.removeprefix("data:").split(";", 1)[0].strip().lower()
        if mime and not mime.startswith("image/"):
            self.metrics.image_data_non_image_mime += 1
        if ";base64" in header.lower() and len(data) < 2_000_000:
            try:
                base64.b64decode(data, validate=True)
            except Exception:
                self.metrics.image_bad_data_url += 1

    def _mark_current_figure_has_media(self) -> None:
        for index in range(len(self._figure_stack)):
            self._figure_stack[index] = True


def run_audit(
    *,
    zotero_data_dirs: tuple[Path, ...],
    state_db: Path | None,
    output: Path | None = None,
    latest_output: Path | None = None,
    skip_job_check: bool = False,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    job_index = (
        SourceHtmlJobIndex(
            enabled=False,
            counts={},
            succeeded_source_keys=set(),
            succeeded_source_attachment_keys=set(),
            db_errors=[],
        )
        if skip_job_check
        else load_source_html_job_index(state_db)
    )
    db_errors: list[dict[str, str]] = [*job_index.db_errors]
    walk_errors: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []

    for data_dir in zotero_data_dirs:
        attachment_index = _load_html_attachment_index(data_dir, db_errors=db_errors)
        seen_attachment_paths: set[str] = set()
        for html_path in _iter_storage_html_files(data_dir / "storage", walk_errors=walk_errors):
            attachment = attachment_index.get(_safe_resolve_key(html_path))
            if attachment is not None:
                seen_attachment_paths.add(_safe_resolve_key(attachment.file_path))
            record = _audit_html_file(
                data_dir=data_dir,
                html_path=html_path,
                attachment=attachment,
                job_index=job_index,
            )
            records.append(record)
        for safe_path, attachment in attachment_index.items():
            if safe_path in seen_attachment_paths or attachment.file_path.is_file():
                continue
            if not _looks_like_arxiv_html(
                attachment.file_path,
                title=attachment.title,
                zotero_path=attachment.zotero_path,
            ):
                continue
            records.append(_audit_missing_html_attachment(data_dir=data_dir, attachment=attachment))
    _mark_stale_arxiv_html_records(records)

    source_records = [record for record in records if record["is_source_html"]]
    non_source_records = [record for record in records if not record["is_source_html"]]
    duplicate_source_parents = _duplicate_source_parents(source_records)
    critical_records = [
        record for record in records if any(issue in CRITICAL_ISSUES for issue in record["issues"])
    ]
    warning_records = [
        record
        for record in records
        if record["warnings"] or any(issue in WARNING_ISSUES for issue in record["issues"])
    ]
    issue_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    source_kind_counts: Counter[str] = Counter()
    remote_image_hosts: Counter[str] = Counter()
    for record in records:
        issue_counts.update(record["issues"])
        warning_counts.update(record["warnings"])
        warning_counts.update(issue for issue in record["issues"] if issue in WARNING_ISSUES)
    for record in source_records:
        source_kind_counts.update([record["source_kind"] or "unknown"])
        remote_image_hosts.update(dict(record.get("remote_image_hosts", [])))

    report = {
        "summary": {
            "generated_at": generated_at,
            "html_files_total": len(records),
            "source_html_files": len(source_records),
            "non_source_html_files": len(non_source_records),
            "source_html_jobs": job_index.counts,
            "source_html_job_check_enabled": job_index.enabled,
            "source_kind_counts": dict(sorted(source_kind_counts.items())),
            "issue_counts": dict(sorted(issue_counts.items())),
            "warning_counts": dict(sorted(warning_counts.items())),
            "critical_records": len(critical_records),
            "warning_only_records": len(
                [
                    record
                    for record in warning_records
                    if not any(issue in CRITICAL_ISSUES for issue in record["issues"])
                ]
            ),
            "duplicate_source_parent_count": len(duplicate_source_parents),
            "remote_image_hosts_top": remote_image_hosts.most_common(20),
            "walk_errors": walk_errors,
            "db_errors": db_errors,
        },
        "critical_records": critical_records,
        "warning_records": warning_records,
        "duplicate_source_parents": duplicate_source_parents,
        "non_source_html": non_source_records,
        "all_records": records,
    }
    if output is not None:
        _write_json(output, report)
    if latest_output is not None:
        _write_json(latest_output, report)
    return report


def load_source_html_job_index(state_db: Path | None) -> SourceHtmlJobIndex:
    if state_db is None or not state_db.is_file():
        return SourceHtmlJobIndex(
            enabled=False,
            counts={},
            succeeded_source_keys=set(),
            succeeded_source_attachment_keys=set(),
            db_errors=[],
        )
    db_errors: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    succeeded_source_keys: set[tuple[str, str]] = set()
    succeeded_source_attachment_keys: set[str] = set()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(state_db)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select library_id, attachment_key, status, pipeline_key, en_html_path
            from html_jobs
            """
        ).fetchall()
    except sqlite3.Error as exc:
        db_errors.append({"path": str(state_db), "error": str(exc)})
        return SourceHtmlJobIndex(
            enabled=False,
            counts={},
            succeeded_source_keys=set(),
            succeeded_source_attachment_keys=set(),
            db_errors=db_errors,
        )
    finally:
        if connection is not None:
            connection.close()

    source_job_rows = 0
    for row in rows:
        pipeline_key = str(row["pipeline_key"] or "")
        label = _source_html_job_label(pipeline_key)
        if label is None:
            continue
        source_job_rows += 1
        status = str(row["status"] or "unknown")
        counts[f"{label}:{status}"] += 1
        if label == "source_html" and status == "succeeded":
            library_id = str(row["library_id"] or "")
            attachment_key = str(row["attachment_key"] or "")
            if library_id and attachment_key:
                succeeded_source_keys.add((library_id, attachment_key))
            if attachment_key:
                succeeded_source_attachment_keys.add(attachment_key)
    return SourceHtmlJobIndex(
        enabled=source_job_rows > 0,
        counts=dict(sorted(counts.items())),
        succeeded_source_keys=succeeded_source_keys,
        succeeded_source_attachment_keys=succeeded_source_attachment_keys,
        db_errors=db_errors,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit polished Zotero source HTML attachments.")
    parser.add_argument("--zotero-data-dir", action="append", type=Path, default=[])
    parser.add_argument("--zotero-root", action="append", type=Path, default=[])
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--latest-output", type=Path)
    parser.add_argument("--skip-job-check", action="store_true")
    parser.add_argument("--fail-on-critical", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    config = from_env(load_file=True)
    zotero_data_dirs = _resolve_zotero_data_dirs(
        explicit=tuple(args.zotero_data_dir),
        roots=tuple(args.zotero_root),
        config_data_dirs=config.zotero_data_dirs,
        config_roots=config.zotero_discovery_roots,
        max_depth=config.zotero_discovery_max_depth,
    )
    if not zotero_data_dirs:
        print("No Zotero data directories found.", file=sys.stderr)
        return 2

    state_db = args.state_db or _first_existing_path(
        Path("/data/ocr/state.sqlite"),
        config.state_db_path,
        Path("data/ingest/state.sqlite"),
    )
    output = args.output
    latest_output = args.latest_output
    if output is None and latest_output is None:
        diagnostics_dir = config.html_data_root / "diagnostics"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = diagnostics_dir / f"source_html_quality_audit_{timestamp}.json"
        latest_output = diagnostics_dir / "source_html_quality_audit_latest.json"

    report = run_audit(
        zotero_data_dirs=zotero_data_dirs,
        state_db=state_db,
        output=output,
        latest_output=latest_output,
        skip_job_check=args.skip_job_check,
    )
    summary = report["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2 if args.pretty else None))
    if args.fail_on_critical and int(summary["critical_records"]) > 0:
        return 1
    return 0


def _audit_html_file(
    *,
    data_dir: Path,
    html_path: Path,
    attachment: HtmlAttachmentRow | None,
    job_index: SourceHtmlJobIndex,
) -> dict[str, Any]:
    library_id = library_id_for_data_dir(data_dir)
    key = html_path.parent.name
    title = attachment.title if attachment is not None else html_path.stem
    parent_key = attachment.parent_key if attachment is not None else None
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        html = ""
        read_error = str(exc)
    else:
        read_error = ""

    parser = ArticleHtmlAuditParser(base_dir=html_path.parent)
    if html:
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            pass
        parser.finalize(html)

    is_source_html = _looks_like_source_html(html_path, title=title, zotero_path=attachment.zotero_path if attachment else "")
    is_arxiv_html = _looks_like_arxiv_html(html_path, title=title, zotero_path=attachment.zotero_path if attachment else "")
    is_generated_html = _looks_like_generated_html(
        html_path,
        title=title,
        zotero_path=attachment.zotero_path if attachment else "",
    )
    job_ok = (
        (library_id, key) in job_index.succeeded_source_keys or key in job_index.succeeded_source_attachment_keys
        if job_index.enabled
        else None
    )
    issues: list[str] = []
    warnings: list[str] = []
    counts = parser.metrics.as_dict()
    style_rules = _style_rule_presence(html)

    if read_error:
        issues.append("html_read_error")
    if attachment is None and (is_source_html or is_arxiv_html):
        issues.append("missing_zotero_attachment_record")
    if is_source_html:
        if job_index.enabled and not job_ok:
            issues.append("no_succeeded_source_html_job")
        if not parser.style_ok:
            issues.append("missing_web_polish_style")
        if not parser.web_doc_main:
            issues.append("missing_web_doc_main")
        if not parser.source_kind:
            issues.append("missing_source_kind")
        if counts["local_fragment_unresolved"]:
            issues.append("unresolved_local_fragment_links")
        if counts["absolute_fragment_links_resolve_local"]:
            issues.append("absolute_fragment_links_resolve_local")
        if counts["image_missing_src"]:
            issues.append("image_missing_src")
        if counts["image_relative_missing_file"]:
            issues.append("image_relative_missing_file")
        if counts["image_data_non_image_mime"]:
            issues.append("image_data_non_image_mime")
        if counts["image_bad_data_url"]:
            issues.append("image_bad_data_url")
        if counts["picture_inline_data_img_with_source"]:
            issues.append("picture_source_overrides_inline_image")
        if counts["latexml_figure_render_error"]:
            issues.append("latexml_figure_render_error")
        if counts["latexml_rowcolor_artifacts"]:
            issues.append("latexml_rowcolor_artifact")
        if counts["latexml_itemize_marker_blocks"] and not style_rules["latexml_itemize"]:
            issues.append("latexml_itemize_marker_layout")
        if counts["latexml_inline_black_text_styles"]:
            issues.append("latexml_inline_black_text")
        if counts["latexml_black_mathcolor_attrs"]:
            issues.append("latexml_math_black_color")
        if counts["frontiers_reference_buttons"]:
            issues.append("frontiers_reference_button")
        if counts["def_lists"] and not style_rules["def_list"]:
            issues.append("missing_def_list_style")
        if counts["latexml_caption_transformed_blocks"] and not style_rules["latexml_caption"]:
            issues.append("missing_latexml_caption_style")
        if counts["latexml_tables"] and not style_rules["latexml_table"]:
            issues.append("missing_latexml_table_style")
        if (counts["disp_formula_tables"] or counts["display_math_blocks"]) and not style_rules["formula"]:
            issues.append("missing_formula_style")
        if counts["scripts"]:
            issues.append("script_tags_present")
        if counts["table_without_rows"]:
            issues.append("table_without_rows")
        if parser.source_kind == "springer_nature_article" and counts["springer_table_placeholder_markers"]:
            issues.append("springer_table_placeholder")
        if counts["figure_without_media_warning"]:
            warnings.append("figure_without_media_warning")

    if is_source_html or is_generated_html:
        if counts["web_doc_mains"] > 1:
            issues.append("nested_web_doc_wrapper")
        if counts["frontiers_empty_article_reference_links"]:
            issues.append("frontiers_empty_article_reference_link")
        if counts["frontiers_figure_js_controls"]:
            issues.append("frontiers_figure_js_control")
        if counts["pmc_dead_ui_controls"]:
            issues.append("pmc_dead_ui_control")

    return {
        "key": key,
        "library": data_dir.name,
        "library_id": library_id,
        "parent_key": parent_key,
        "title": title,
        "path": str(html_path),
        "is_source_html": is_source_html,
        "is_arxiv_html": is_arxiv_html,
        "is_generated_html": is_generated_html,
        "source_kind": parser.source_kind or "unknown",
        "text_length": parser.text_length,
        "job_ok": job_ok,
        "style_ok": parser.style_ok,
        "style_rules": style_rules,
        "web_doc_main": parser.web_doc_main,
        "counts": counts,
        "samples": {
            "unresolved_local_fragments": parser.unresolved_local_fragments,
            "relative_missing_images": parser.relative_missing_images,
        },
        "remote_image_hosts": parser.remote_image_hosts.most_common(20),
        "issues": issues,
        "warnings": warnings,
        "read_error": read_error,
    }


def _audit_missing_html_attachment(*, data_dir: Path, attachment: HtmlAttachmentRow) -> dict[str, Any]:
    is_source_html = _looks_like_source_html(
        attachment.file_path,
        title=attachment.title,
        zotero_path=attachment.zotero_path,
    )
    is_arxiv_html = _looks_like_arxiv_html(
        attachment.file_path,
        title=attachment.title,
        zotero_path=attachment.zotero_path,
    )
    is_generated_html = _looks_like_generated_html(
        attachment.file_path,
        title=attachment.title,
        zotero_path=attachment.zotero_path,
    )
    return {
        "key": attachment.key,
        "library": data_dir.name,
        "library_id": library_id_for_data_dir(data_dir),
        "parent_key": attachment.parent_key,
        "title": attachment.title,
        "path": str(attachment.file_path),
        "is_source_html": is_source_html,
        "is_arxiv_html": is_arxiv_html,
        "is_generated_html": is_generated_html,
        "source_kind": "unknown",
        "text_length": 0,
        "job_ok": None,
        "style_ok": False,
        "style_rules": _style_rule_presence(""),
        "web_doc_main": False,
        "counts": HtmlMetrics().as_dict(),
        "samples": {
            "unresolved_local_fragments": [],
            "relative_missing_images": [],
        },
        "remote_image_hosts": [],
        "issues": ["html_attachment_missing_file"],
        "warnings": [],
        "read_error": "attachment_file_missing",
    }


def _load_html_attachment_index(data_dir: Path, *, db_errors: list[dict[str, str]]) -> dict[str, HtmlAttachmentRow]:
    db_path = data_dir / "zotero.sqlite"
    storage_dir = data_dir / "storage"
    if not db_path.is_file():
        db_errors.append({"path": str(db_path), "error": "zotero.sqlite missing"})
        return {}
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            select
              i.key,
              parent.key as parentKey,
              ia.path,
              coalesce(title_data.value, '') as title
            from itemAttachments ia
            join items i on i.itemID = ia.itemID
            left join items parent on parent.itemID = ia.parentItemID
            left join deletedItems di on di.itemID = i.itemID
            left join (
              select d.itemID, v.value
              from itemData d
              join fields f on f.fieldID = d.fieldID
              join itemDataValues v on v.valueID = d.valueID
              where f.fieldName = 'title'
            ) title_data on title_data.itemID = i.itemID
            where di.itemID is null
              and (
                lower(coalesce(ia.contentType, '')) in ('text/html', 'application/xhtml+xml')
                or lower(coalesce(ia.path, '')) like '%.html%'
                or lower(coalesce(ia.path, '')) like '%.htm%'
              )
            """
        ).fetchall()
    except sqlite3.Error as exc:
        db_errors.append({"path": str(db_path), "error": str(exc)})
        return {}
    finally:
        try:
            connection.close()
        except Exception:
            pass

    by_path: dict[str, HtmlAttachmentRow] = {}
    for row in rows:
        key = str(row["key"])
        zotero_path = str(row["path"] or "")
        file_path = _resolve_zotero_html_path(storage_dir=storage_dir, key=key, zotero_path=zotero_path)
        attachment = HtmlAttachmentRow(
            key=key,
            parent_key=str(row["parentKey"]) if row["parentKey"] else None,
            title=str(row["title"] or file_path.stem),
            zotero_path=zotero_path,
            file_path=file_path,
        )
        by_path[_safe_resolve_key(file_path)] = attachment
    return by_path


def _iter_storage_html_files(storage_dir: Path, *, walk_errors: list[dict[str, str]]) -> Iterable[Path]:
    if not storage_dir.is_dir():
        return
    stack = [storage_dir]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda path: path.name.lower(), reverse=True)
        except OSError as exc:
            walk_errors.append({"path": str(current), "error": str(exc)})
            continue
        for child in children:
            try:
                if child.is_dir():
                    stack.append(child)
                elif child.is_file() and child.suffix.lower() in LOCAL_HTML_SUFFIXES:
                    yield child
            except OSError as exc:
                walk_errors.append({"path": str(child), "error": str(exc)})


def _resolve_zotero_html_path(*, storage_dir: Path, key: str, zotero_path: str) -> Path:
    if zotero_path.startswith("storage:"):
        return storage_dir / key / zotero_path.removeprefix("storage:")
    if zotero_path:
        return Path(zotero_path)
    folder = storage_dir / key
    try:
        candidates = sorted(
            (child for child in folder.iterdir() if child.is_file() and child.suffix.lower() in LOCAL_HTML_SUFFIXES),
            key=lambda child: child.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        candidates = []
    return candidates[0] if candidates else folder / f"{key}.html"


def _resolve_zotero_data_dirs(
    *,
    explicit: tuple[Path, ...],
    roots: tuple[Path, ...],
    config_data_dirs: tuple[Path, ...],
    config_roots: tuple[Path, ...],
    max_depth: int,
) -> tuple[Path, ...]:
    if explicit:
        return unique_paths(tuple(path.expanduser() for path in explicit if is_zotero_data_dir(path.expanduser())))
    discovery_roots = roots or config_roots
    discovered = discover_zotero_data_dirs(discovery_roots, max_depth=max_depth)
    if discovered:
        return discovered
    return tuple(path for path in config_data_dirs if is_zotero_data_dir(path))


def _duplicate_source_parents(source_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_parent: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in source_records:
        parent_key = str(record.get("parent_key") or "")
        if not parent_key:
            continue
        by_parent[(str(record.get("library_id") or ""), parent_key)].append(record)
    duplicates: list[dict[str, Any]] = []
    for (library_id, parent_key), records in sorted(by_parent.items()):
        if len(records) < 2:
            continue
        duplicates.append(
            {
                "library_id": library_id,
                "parent_key": parent_key,
                "source_count": len(records),
                "attachments": [
                    {
                        "key": record["key"],
                        "title": record["title"],
                        "path": record["path"],
                    }
                    for record in records
                ],
            }
        )
    return duplicates


def _mark_stale_arxiv_html_records(records: list[dict[str, Any]]) -> None:
    active_source_parents = {
        (str(record.get("library_id") or ""), str(record.get("parent_key") or ""))
        for record in records
        if record.get("is_source_html")
        and record.get("parent_key")
        and "missing_zotero_attachment_record" not in record.get("issues", [])
    }
    for record in records:
        parent_key = str(record.get("parent_key") or "")
        if not parent_key or not record.get("is_arxiv_html"):
            continue
        parent_id = (str(record.get("library_id") or ""), parent_key)
        if parent_id not in active_source_parents:
            continue
        issues = record.setdefault("issues", [])
        if "stale_arxiv_html_attachment" not in issues:
            issues.append("stale_arxiv_html_attachment")


def _source_html_job_label(pipeline_key: str) -> str | None:
    if pipeline_key == "source_html" or ("source_html=1" in pipeline_key and "en=1" in pipeline_key):
        return "source_html"
    if pipeline_key == "source_html_translate" or (
        "source_html=1" in pipeline_key and "ru=1" in pipeline_key and "en=1" not in pipeline_key
    ):
        return "source_html_translate"
    return None


def _looks_like_source_html(path: Path, *, title: str, zotero_path: str) -> bool:
    haystack = f"{path.name} {title} {zotero_path}".casefold()
    if SOURCE_HTML_RE.search(haystack) is not None:
        return True
    match = LANGUAGE_HTML_RE.search(haystack)
    return bool(match and _is_source_html_language_marker(match.group(1)))


def _looks_like_arxiv_html(path: Path, *, title: str, zotero_path: str) -> bool:
    haystack = f"{path.name} {title} {zotero_path}".casefold()
    return ARXIV_HTML_RE.search(haystack) is not None


def _looks_like_generated_html(path: Path, *, title: str, zotero_path: str) -> bool:
    haystack = f"{path.name} {title} {zotero_path}".casefold()
    return (
        GENERATED_HTML_RE.search(haystack) is not None
        and not _looks_like_source_html(path, title=title, zotero_path=zotero_path)
        and ARXIV_HTML_RE.search(haystack) is None
    )


def _is_source_html_language_marker(value: str) -> bool:
    marker = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return marker not in {"ru", "source", "arxiv", "ocr", "fulltext", "pdf", "html"}


def _is_frontiers_figure_js_control(tag_name: str, attr_map: dict[str, str]) -> bool:
    if tag_name != "button":
        return False
    class_name = attr_map.get("class", "")
    data_event = attr_map.get("data-event", "")
    aria_label = attr_map.get("aria-label", "")
    if "ArticleFigure__figureButton" in class_name:
        return True
    if "ButtonIcon" in class_name and data_event.startswith(("articleFigure-", "articleTable-")):
        return True
    return bool("ButtonIcon" in class_name and aria_label.startswith(("Expand ", "Download ")))


def _is_pmc_dead_ui_control(tag_name: str, attr_map: dict[str, str]) -> bool:
    class_name = attr_map.get("class", "")
    element_id = attr_map.get("id", "")
    aria_label = attr_map.get("aria-label", "")
    aria_controls = attr_map.get("aria-controls", "")
    if tag_name == "form" and element_id == "collections-action-dialog-form":
        return True
    if tag_name == "button" and aria_label == "Show article permalink":
        return True
    if tag_name == "button" and aria_controls == "journal_context_menu":
        return True
    if tag_name == "button" and "d-button" in class_name.split() and aria_controls:
        return True
    return any(
        token in class_name
        for token in (
            "citation-dialog-trigger",
            "collections-dialog-trigger",
            "collections-action-panel-form",
            "export-button",
            "pmc-permalink__dropdown__copy__btn",
            "usa-accordion__button",
        )
    )


def _style_rule_presence(html: str) -> dict[str, bool]:
    return {
        "def_list": "dl.def-list" in html,
        "latexml_caption": ".ltx_caption .ltx_transformed_outer" in html,
        "latexml_table": "figure.ltx_table > figcaption" in html
        and "figure.ltx_table table" in html,
        "latexml_itemize": ".ltx_item > .ltx_tag_item" in html,
        "formula": "table.disp-formula td.label" in html,
    }


def _html_without_ignored_blocks(html: str) -> str:
    return IGNORED_BLOCK_RE.sub("", html)


def _is_data_url(value: str) -> bool:
    return value[:5].lower() == "data:"


def _resolve_relative_asset(src: str, *, base_dir: Path) -> Path | None:
    clean_src = src.split("?", 1)[0].split("#", 1)[0]
    if not clean_src:
        return None
    decoded = urllib.parse.unquote(clean_src).replace("\\", "/")
    if decoded.startswith("/"):
        decoded = decoded.lstrip("/")
    return base_dir / Path(*[part for part in decoded.split("/") if part])


def _figure_tag_has_intrinsic_content(attr_map: dict[str, str]) -> bool:
    class_value = attr_map.get("class", "").casefold()
    return "ltx_table" in class_value or "ltx_lstlisting" in class_value


def _looks_like_supplementary_resource_href(href: str) -> bool:
    parsed = urlsplit_or_none(href)
    if parsed is None:
        return False
    path = parsed.path or href.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(urllib.parse.unquote(path)).suffix.casefold()
    if suffix in SUPPLEMENTARY_RESOURCE_SUFFIXES:
        return True
    lowered = href.casefold()
    return any(
        f"{suffix}?" in lowered or f"{suffix}#" in lowered
        for suffix in SUPPLEMENTARY_RESOURCE_SUFFIXES
    )


def _safe_resolve_key(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_existing_path(*paths: Path) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return paths[0] if paths else None


if __name__ == "__main__":
    raise SystemExit(main())

"""Recover failed arXiv LaTeXML figures from the original arXiv source package."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from html import escape as html_escape
from html import unescape
import gzip
import io
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from typing import Protocol

from zotero_metadata_enrichment.safe_http import (
    host_suffix_redirect,
    safe_urlopen,
)

from .html_links import _arxiv_abs_parts, _arxiv_html_parts, _urlsplit_or_none
from .web_polish.core import HTML_TAG_RE, attr_value, balanced_element_from_match, visible_text


SourceFetcher = Callable[[str], bytes]


class FigureRenderer(Protocol):
    def render(
        self,
        *,
        source_dir: Path,
        figure: SourceFigure,
        arxiv_id: str,
        index: int,
    ) -> bytes | None: ...

_FIGURE_ENV_RE = re.compile(r"\\begin\{figure\*?\}[\s\S]*?\\end\{figure\*?\}", re.IGNORECASE)
_FIGURE_ENV_WITH_BODY_RE = re.compile(r"\\begin\{figure\*?\}(?:\s*\[[^\]]*\])?(?P<body>[\s\S]*?)\\end\{figure\*?\}", re.IGNORECASE)
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]+)\}")
_BEGIN_ENV_RE = re.compile(r"\\begin\{([^{}]+)\}")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")
_SECTION_RE = re.compile(r"\\(?P<kind>section|subsection|subsubsection)\*?(?:\s*\[[^\]]*\])?\s*\{")
_BIBITEM_RE = re.compile(r"\\bibitem(?:\[(?P<label>[^\]]*)\])?\s*\{(?P<key>[^{}]+)\}")
_LATEX_COMMAND_WITH_ARG_RE = re.compile(r"\\[a-zA-Z@]+\*?(?:\s*\[[^\]]*\])?\s*\{([^{}]*)\}")
_LATEX_COMMAND_RE = re.compile(r"\\[a-zA-Z@]+\*?")
_LTX_ERROR_RE = re.compile(r"ltx_ERROR[^>]*>\s*(?P<text>[^<]+)", re.IGNORECASE)
_CAPTION_TAG_RE = re.compile(r"<figcaption\b[^>]*>[\s\S]*?</figcaption>", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"(?i)(?:arxiv:|10\.48550/arxiv\.)([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?")


_MAX_SOURCE_PACKAGE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_ARCHIVE_ENTRIES = 4096
_MAX_SOURCE_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_EXTRACTED_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_TEXT_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_TEXT_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FIGURES = 2048
_MAX_RENDERED_PNG_BYTES = 8 * 1024 * 1024
_MAX_RENDERED_PIXELS = 16_000_000


@dataclass
class _ExtractionBudget:
    max_entries: int = _MAX_SOURCE_ARCHIVE_ENTRIES
    max_member_bytes: int = _MAX_SOURCE_MEMBER_BYTES
    max_total_bytes: int = _MAX_SOURCE_EXTRACTED_BYTES
    entries: int = 0
    extracted_bytes: int = 0

    def observe_entry(self, declared_size: int) -> None:
        self.entries += 1
        if self.entries > max(self.max_entries, 0):
            raise ValueError(f"arXiv source archive exceeds {max(self.max_entries, 0)} entries")
        if declared_size > max(self.max_member_bytes, 0):
            raise ValueError(
                f"arXiv source member exceeds {max(self.max_member_bytes, 0)} bytes"
            )
        if self.extracted_bytes + max(declared_size, 0) > max(self.max_total_bytes, 0):
            raise ValueError(
                f"arXiv source archive exceeds {max(self.max_total_bytes, 0)} extracted bytes"
            )

    def consume(self, byte_count: int) -> None:
        self.extracted_bytes += max(byte_count, 0)
        if self.extracted_bytes > max(self.max_total_bytes, 0):
            raise ValueError(
                f"arXiv source archive exceeds {max(self.max_total_bytes, 0)} extracted bytes"
            )


@dataclass
class _TextReadBudget:
    max_total_bytes: int = _MAX_SOURCE_TEXT_TOTAL_BYTES
    consumed_bytes: int = 0

    def read(self, path: Path, *, max_bytes: int = _MAX_SOURCE_TEXT_BYTES) -> str | None:
        byte_limit = min(
            max(max_bytes, 0),
            max(self.max_total_bytes - self.consumed_bytes, 0),
        )
        payload = _read_file_bounded(path, max_bytes=byte_limit)
        if payload is None:
            return None
        self.consumed_bytes += len(payload)
        return payload.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ArxivSourceRecoveryResult:
    html: str
    recovered_figures: int = 0
    attempted_figures: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrokenLatexmlFigure:
    start: int
    end: int
    fragment: str
    open_tag: str
    figure_id: str
    caption_html: str
    caption_text: str
    error_tokens: tuple[str, ...]


@dataclass(frozen=True)
class SourceFigure:
    relative_path: str
    tex: str
    caption_text: str
    labels: tuple[str, ...]
    environments: tuple[str, ...]


class LatexSourceFigureRenderer:
    """Render one source figure to PNG via local TeX and PyMuPDF."""

    def __init__(
        self,
        *,
        tex_command: str | None = None,
        timeout_seconds: int | None = None,
        scale: float = 1.5,
    ) -> None:
        self.tex_command = tex_command or os.environ.get("ARXIV_SOURCE_RECOVERY_TEX_COMMAND", "pdflatex")
        self.tex_docker_image = os.environ.get("ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE", "").strip()
        self.timeout_seconds = timeout_seconds or _env_int("ARXIV_SOURCE_RECOVERY_TIMEOUT_SECONDS", 120)
        self.scale = scale

    def render(
        self,
        *,
        source_dir: Path,
        figure: SourceFigure,
        arxiv_id: str,
        index: int,
    ) -> bytes | None:
        del arxiv_id
        main_tex = _find_main_tex(source_dir)
        standalone_name = f"_z2m_arxiv_source_recovery_{index}.tex"
        standalone_path = source_dir / standalone_name
        aux_path = standalone_path.with_suffix(".aux")
        pdf_path = standalone_path.with_suffix(".pdf")
        standalone_document = _standalone_figure_document(
            source_dir=source_dir,
            main_tex=main_tex,
            figure=figure,
        )
        synthesized_aux = _synthesized_aux(source_dir=source_dir, main_tex=main_tex)
        if not _write_text_file_bounded(standalone_path, standalone_document):
            return None
        if not _write_text_file_bounded(aux_path, synthesized_aux):
            return None

        command = self._render_command(source_dir=source_dir, standalone_name=standalone_name)
        try:
            completed = subprocess.run(
                command,
                cwd=source_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                env=_restricted_tex_environment(),
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not pdf_path.is_file():
            return None
        return _pdf_first_page_to_png(pdf_path, scale=self.scale)

    def _render_command(self, *, source_dir: Path, standalone_name: str) -> list[str]:
        tex_parts = [
            *shlex.split(self.tex_command),
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            standalone_name,
        ]
        if not self.tex_docker_image:
            return tex_parts
        mount_path = str(source_dir.resolve())
        return [
            "docker",
            "run",
            "--rm",
            "-e",
            "openin_any=p",
            "-e",
            "openout_any=p",
            "-v",
            f"{mount_path}:/work",
            "-w",
            "/work",
            self.tex_docker_image,
            *tex_parts,
        ]

    def has_available_engine(self) -> bool:
        if self.tex_docker_image:
            return shutil.which("docker") is not None
        first = shlex.split(self.tex_command)[0] if self.tex_command.strip() else "pdflatex"
        return shutil.which(first) is not None


def recover_latexml_figures_from_arxiv_source_html(
    html: str,
    *,
    source_url: str | None,
    fetch_source: SourceFetcher | None = None,
    renderer: FigureRenderer | None = None,
    enabled: bool | None = None,
    max_figures: int | None = None,
) -> ArxivSourceRecoveryResult:
    """Replace failed LaTeXML figure bodies using arXiv source-derived images.

    The function is deliberately conservative: failures return the original HTML
    with an error note in the result instead of breaking ordinary web polish.
    """

    if enabled is None:
        enabled = _env_bool("ARXIV_SOURCE_RECOVERY_ENABLED", True)
    if not enabled or "ltx_ERROR" not in html:
        return ArxivSourceRecoveryResult(html=html)

    arxiv_id = _arxiv_id_from_source_url(source_url)
    if not arxiv_id:
        return ArxivSourceRecoveryResult(html=html)

    broken_figures = _find_broken_latexml_figures(html)
    if not broken_figures:
        return ArxivSourceRecoveryResult(html=html)

    figure_limit = max_figures or _env_int("ARXIV_SOURCE_RECOVERY_MAX_FIGURES", 4)
    broken_figures = broken_figures[: max(figure_limit, 1)]
    errors: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="z2m_arxiv_source_recovery_") as temp_dir:
            source_dir = Path(temp_dir) / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            source_blob = (fetch_source or fetch_arxiv_source_package)(arxiv_id)
            if len(source_blob) > _MAX_SOURCE_PACKAGE_BYTES:
                raise ValueError(
                    f"arXiv source package exceeds {_MAX_SOURCE_PACKAGE_BYTES} bytes"
                )
            extract_arxiv_source_package(source_blob, source_dir)
            source_figures = collect_source_figures(source_dir)
            if not source_figures:
                return ArxivSourceRecoveryResult(
                    html=html,
                    attempted_figures=len(broken_figures),
                    errors=("no_source_figures",),
                )
            active_renderer = renderer or LatexSourceFigureRenderer()
            replacements: list[tuple[int, int, str]] = []
            recovered = 0
            for index, broken in enumerate(broken_figures, start=1):
                source_figure = select_source_figure(broken, source_figures)
                if source_figure is None:
                    errors.append(f"no_matching_source_figure:{broken.figure_id or index}")
                    continue
                try:
                    png_bytes = active_renderer.render(
                        source_dir=source_dir,
                        figure=source_figure,
                        arxiv_id=arxiv_id,
                        index=index,
                    )
                except Exception as exc:  # pragma: no cover - defensive around external renderers.
                    errors.append(f"render_error:{broken.figure_id or index}:{exc}")
                    continue
                if not png_bytes:
                    errors.append(f"render_failed:{broken.figure_id or index}:{source_figure.relative_path}")
                    continue
                if len(png_bytes) > _MAX_RENDERED_PNG_BYTES:
                    errors.append(
                        f"render_too_large:{broken.figure_id or index}:"
                        f"{len(png_bytes)}>{_MAX_RENDERED_PNG_BYTES}"
                    )
                    continue
                replacements.append(
                    (
                        broken.start,
                        broken.end,
                        _recovered_figure_html(
                            broken=broken,
                            source_figure=source_figure,
                            arxiv_id=arxiv_id,
                            png_bytes=png_bytes,
                        ),
                    )
                )
                recovered += 1
    except Exception as exc:  # pragma: no cover - network/archive edge cases are environment dependent.
        return ArxivSourceRecoveryResult(
            html=html,
            attempted_figures=len(broken_figures),
            errors=(f"source_recovery_error:{exc}",),
        )

    if not replacements:
        return ArxivSourceRecoveryResult(
            html=html,
            attempted_figures=len(broken_figures),
            errors=tuple(errors),
        )

    recovered_html = html
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        recovered_html = recovered_html[:start] + replacement + recovered_html[end:]
    return ArxivSourceRecoveryResult(
        html=recovered_html,
        recovered_figures=recovered,
        attempted_figures=len(broken_figures),
        errors=tuple(errors),
    )


def fetch_arxiv_source_package(arxiv_id: str, *, max_bytes: int = _MAX_SOURCE_PACKAGE_BYTES) -> bytes:
    timeout = _env_int("ARXIV_SOURCE_RECOVERY_FETCH_TIMEOUT_SECONDS", 60)
    quoted_id = urllib.parse.quote(arxiv_id, safe="/")
    request = urllib.request.Request(
        f"https://arxiv.org/e-print/{quoted_id}",
        headers={"User-Agent": "Mozilla/5.0 z2m-arxiv-source-recovery"},
    )
    with safe_urlopen(
        request,
        timeout=timeout,
        redirect_validator=host_suffix_redirect("arxiv.org"),
    ) as response:
        _require_arxiv_response_url(response, fallback=request.full_url)
        byte_limit = max(max_bytes, 0)
        declared = _response_content_length(response)
        if declared is not None and declared > byte_limit:
            raise ValueError(f"arXiv source package exceeds {byte_limit} bytes")
        raw_blob = response.read(byte_limit + 1)
        if not isinstance(raw_blob, bytes):
            raise TypeError("arXiv source response did not return bytes.")
        blob = raw_blob
    if len(blob) > byte_limit:
        raise ValueError(f"arXiv source package exceeds {byte_limit} bytes")
    return blob


def extract_arxiv_source_package(
    blob: bytes,
    destination: Path,
    *,
    max_package_bytes: int = _MAX_SOURCE_PACKAGE_BYTES,
    max_entries: int = _MAX_SOURCE_ARCHIVE_ENTRIES,
    max_member_bytes: int = _MAX_SOURCE_MEMBER_BYTES,
    max_total_bytes: int = _MAX_SOURCE_EXTRACTED_BYTES,
) -> None:
    if len(blob) > max(max_package_bytes, 0):
        raise ValueError(f"arXiv source package exceeds {max(max_package_bytes, 0)} bytes")
    destination.mkdir(parents=True, exist_ok=True)
    budget = _ExtractionBudget(
        max_entries=max_entries,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
    )
    buffer = io.BytesIO(blob)
    try:
        tar_archive = tarfile.open(fileobj=buffer, mode="r:*")
    except tarfile.TarError:
        tar_archive = None
    if tar_archive is not None:
        with tar_archive:
            for tar_member in tar_archive:
                _safe_extract_tar_member(tar_archive, tar_member, destination, budget=budget)
        return

    buffer = io.BytesIO(blob)
    if zipfile.is_zipfile(buffer):
        with zipfile.ZipFile(buffer) as zip_archive:
            for zip_member in zip_archive.infolist():
                _safe_extract_zip_member(zip_archive, zip_member, destination, budget=budget)
        return

    target = destination / "main.tex"
    if blob.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as source:
            budget.observe_entry(0)
            _copy_stream_bounded(source, target, budget=budget, max_bytes=max_member_bytes)
        return
    budget.observe_entry(len(blob))
    _copy_stream_bounded(io.BytesIO(blob), target, budget=budget, max_bytes=max_member_bytes)


def collect_source_figures(
    source_dir: Path,
    *,
    max_text_bytes: int = _MAX_SOURCE_TEXT_BYTES,
    max_total_text_bytes: int = _MAX_SOURCE_TEXT_TOTAL_BYTES,
    max_figures: int = _MAX_SOURCE_FIGURES,
) -> list[SourceFigure]:
    figures: list[SourceFigure] = []
    source_root = source_dir.resolve(strict=False)
    budget = _TextReadBudget(max_total_bytes=max_total_text_bytes)
    figure_limit = max(max_figures, 0)
    if figure_limit == 0:
        return figures
    for path in sorted(source_dir.rglob("*.tex")):
        if not _is_within_root(path, source_root):
            continue
        text = budget.read(path, max_bytes=max_text_bytes)
        if text is None:
            continue
        matches = list(_FIGURE_ENV_RE.finditer(text))
        if not matches:
            continue
        for match in matches:
            figure_tex = match.group(0)
            prefix = text[: match.start()]
            if len(matches) == 1 and len(prefix.strip()) <= 20_000:
                figure_tex = f"{prefix}{figure_tex}"
            caption = _latex_visible_text(_extract_latex_caption(figure_tex))
            labels = tuple(_LABEL_RE.findall(figure_tex))
            environments = tuple(
                sorted(
                    {
                        env.strip().lower().rstrip("*")
                        for env in _BEGIN_ENV_RE.findall(figure_tex)
                        if env.strip().lower().rstrip("*") not in {"figure"}
                    }
                )
            )
            figures.append(
                SourceFigure(
                    relative_path=_relative_source_path(path, source_dir),
                    tex=figure_tex,
                    caption_text=caption,
                    labels=labels,
                    environments=environments,
                )
            )
            if len(figures) >= figure_limit:
                return figures
    return figures


def select_source_figure(
    broken: BrokenLatexmlFigure,
    candidates: list[SourceFigure],
) -> SourceFigure | None:
    scored = [
        (_source_figure_score(broken, candidate), candidate)
        for candidate in candidates
    ]
    scored = [(score, candidate) for score, candidate in scored if score > 0]
    if not scored:
        return None
    return max(scored, key=lambda item: item[0])[1]


def _find_broken_latexml_figures(html: str) -> list[BrokenLatexmlFigure]:
    figures: list[BrokenLatexmlFigure] = []
    for match in HTML_TAG_RE.finditer(html):
        raw_tag = match.group(0)
        if raw_tag.startswith("</") or match.group("tag").lower() != "figure":
            continue
        fragment = balanced_element_from_match(html, match)
        if fragment is None or "ltx_ERROR" not in fragment:
            continue
        caption_match = _CAPTION_TAG_RE.search(fragment)
        caption_html = caption_match.group(0) if caption_match is not None else ""
        figures.append(
            BrokenLatexmlFigure(
                start=match.start(),
                end=match.start() + len(fragment),
                fragment=fragment,
                open_tag=raw_tag,
                figure_id=attr_value(match.group("attrs") or "", "id") or "",
                caption_html=caption_html,
                caption_text=visible_text(caption_html),
                error_tokens=_latexml_error_tokens(fragment),
            )
        )
    return figures


def _latexml_error_tokens(fragment: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for match in _LTX_ERROR_RE.finditer(fragment):
        text = unescape(match.group("text")).strip()
        tokens.extend(re.findall(r"[A-Za-z][A-Za-z0-9_:-]*\*?", text))
    return tuple(dict.fromkeys(token.lower().rstrip("*") for token in tokens))


def _source_figure_score(broken: BrokenLatexmlFigure, candidate: SourceFigure) -> int:
    score = 0
    body_probe = candidate.tex.lower()
    candidate_envs = set(candidate.environments)
    for token in broken.error_tokens:
        if token in candidate_envs:
            score += 1000
        elif f"\\begin{{{token}}}" in body_probe:
            score += 850
        elif token and token in body_probe:
            score += 150
    caption_score = _token_overlap_score(broken.caption_text, candidate.caption_text)
    score += caption_score
    if broken.figure_id and any(broken.figure_id.casefold() in label.casefold() for label in candidate.labels):
        score += 200
    if not broken.error_tokens and caption_score == 0:
        return 0
    return score


def _token_overlap_score(left: str, right: str) -> int:
    left_tokens = _meaningful_tokens(left)
    right_tokens = _meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    overlap = left_tokens & right_tokens
    if not overlap:
        return 0
    return int(500 * len(overlap) / max(len(left_tokens), len(right_tokens)))


def _meaningful_tokens(text: str) -> set[str]:
    stopwords = {
        "and",
        "are",
        "for",
        "figure",
        "fig",
        "into",
        "the",
        "this",
        "with",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", text.casefold())
        if token not in stopwords
    }


def _recovered_figure_html(
    *,
    broken: BrokenLatexmlFigure,
    source_figure: SourceFigure,
    arxiv_id: str,
    png_bytes: bytes,
) -> str:
    src = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    alt_text = broken.caption_text or source_figure.caption_text or "Recovered arXiv source figure"
    wrapper = (
        '<div class="z2m-source-recovered-figure ltx_align_center"'
        ' data-z2m-recovery="arxiv-source"'
        f' data-z2m-source-arxiv-id="{html_escape(arxiv_id, quote=True)}"'
        f' data-z2m-source-path="{html_escape(source_figure.relative_path, quote=True)}">'
        '<img class="z2m-source-recovered-image"'
        f' src="{src}"'
        f' alt="{html_escape(alt_text, quote=True)}"'
        ' style="max-width:100%;height:auto;display:block;margin:0 auto;"/>'
        "</div>"
    )
    return f"{broken.open_tag}\n{wrapper}\n{broken.caption_html}\n</figure>"


def _arxiv_id_from_source_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = _urlsplit_or_none(source_url)
    if parsed is not None:
        for extractor in (_arxiv_html_parts, _arxiv_abs_parts):
            parts = extractor(parsed)
            if parts is not None:
                return parts[0]
    match = _ARXIV_ID_RE.search(source_url)
    return match.group(1) if match is not None else None


def _standalone_figure_document(
    *,
    source_dir: Path,
    main_tex: Path | None,
    figure: SourceFigure,
) -> str:
    del source_dir
    return "\n".join(
        part
        for part in (
            _minimal_figure_preamble(),
            _source_macro_preamble(main_tex),
            _preview_and_caption_suppression(),
            r"\begin{document}",
            _renderable_figure_tex(figure.tex),
            r"\end{document}",
            "",
        )
        if part
    )


def _minimal_figure_preamble() -> str:
    return r"""
\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{xcolor}
\usepackage{graphicx}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{booktabs}
\usepackage{multirow}
\usepackage{adjustbox}
\usepackage[numbers]{natbib}
\usepackage[edges]{forest}
\usepackage{tikz}
\usepackage{hyperref}
\pagestyle{empty}
\providecommand{\eg}{\textit{e.g.,}}
\providecommand{\ie}{\textit{i.e.,}}
""".strip()


def _source_macro_preamble(main_tex: Path | None) -> str:
    if main_tex is None or not main_tex.is_file():
        return ""
    payload = _read_file_bounded(main_tex, max_bytes=_MAX_SOURCE_TEXT_BYTES)
    if payload is None:
        return ""
    text = payload.decode("utf-8", errors="replace")
    preamble = text.split(r"\begin{document}", 1)[0]
    lines: list[str] = []
    for raw_line in preamble.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%"):
            continue
        if line.startswith((r"\definecolor", r"\colorlet", r"\usetikzlibrary", r"\tikzset")):
            lines.append(raw_line)
        elif line.startswith((r"\newcommand", r"\renewcommand", r"\providecommand", r"\DeclareMathOperator")):
            lines.append(raw_line.replace(r"\newcommand", r"\providecommand", 1))
    return "\n".join(lines)


def _preview_and_caption_suppression() -> str:
    return r"""
\usepackage[active,tightpage]{preview}
\PreviewEnvironment{figure}
\PreviewEnvironment{figure*}
\PreviewEnvironment{center}
\PreviewEnvironment{tikzpicture}
\PreviewBorder=12pt
\makeatletter
\long\def\caption{\@ifnextchar[{\z2m@caption@opt}{\z2m@caption@plain}}
\long\def\z2m@caption@opt[#1]#2{}
\long\def\z2m@caption@plain#1{}
\makeatother
""".strip()


def _renderable_figure_tex(tex: str) -> str:
    """Convert source float figures into renderable standalone content.

    arXiv source snippets often use figure/figure* floats. Floats are useful in
    full papers but fragile in our isolated renderer, and captions are preserved
    from the polished HTML instead of the rendered image.
    """

    match = _FIGURE_ENV_WITH_BODY_RE.search(tex)
    if match is None:
        return tex
    setup = tex[: match.start()].strip()
    body = match.group("body").strip()
    parts = [part for part in (setup, r"\begin{center}", body, r"\end{center}") if part]
    return "\n".join(parts)


def _synthesized_aux(*, source_dir: Path, main_tex: Path | None) -> str:
    lines = [r"\relax"]
    lines.extend(_synthesized_section_labels(source_dir=source_dir, main_tex=main_tex))
    lines.extend(_synthesized_bibcites(source_dir))
    return "\n".join(lines) + "\n"


def _synthesized_section_labels(*, source_dir: Path, main_tex: Path | None) -> list[str]:
    if main_tex is None or not main_tex.is_file():
        return []
    document = _expanded_tex_document(main_tex, source_dir=source_dir)
    counters = [0, 0, 0]
    current = ""
    labels: list[str] = []
    for raw_line in document.splitlines():
        line = _strip_latex_comment(raw_line)
        for match in _SECTION_RE.finditer(line):
            kind = match.group("kind")
            if kind == "section":
                counters[0] += 1
                counters[1] = 0
                counters[2] = 0
            elif kind == "subsection":
                counters[1] += 1
                counters[2] = 0
            elif kind == "subsubsection":
                counters[2] += 1
            current = ".".join(str(value) for value in counters if value)
        for label in _LABEL_RE.findall(line):
            if not current:
                continue
            labels.append(rf"\newlabel{{{label}}}{{{{{current}}}{{}}}}")
    return labels


def _expanded_tex_document(
    path: Path,
    *,
    source_dir: Path,
    seen: set[Path] | None = None,
    _budget: _TextReadBudget | None = None,
) -> str:
    if seen is None:
        seen = set()
    source_root = source_dir.resolve(strict=False)
    path = path.resolve(strict=False)
    if path in seen or not _is_within_root(path, source_root):
        return ""
    seen.add(path)
    budget = _budget or _TextReadBudget()
    text = budget.read(path)
    if text is None:
        return ""

    def replace_input(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        child = (path.parent / raw).with_suffix(".tex") if not raw.endswith(".tex") else path.parent / raw
        if not _is_within_root(child, source_root) or not child.is_file():
            child = (source_dir / raw).with_suffix(".tex") if not raw.endswith(".tex") else source_dir / raw
        return _expanded_tex_document(
            child,
            source_dir=source_dir,
            seen=seen,
            _budget=budget,
        )

    return _INPUT_RE.sub(replace_input, text)


def _synthesized_bibcites(source_dir: Path) -> list[str]:
    bbl_files = sorted(source_dir.glob("*.bbl"))
    if not bbl_files:
        return []
    budget = _TextReadBudget()
    texts = [text for path in bbl_files if (text := budget.read(path)) is not None]
    text = "\n".join(texts)
    lines: list[str] = []
    for index, match in enumerate(_BIBITEM_RE.finditer(text), start=1):
        label = match.group("label") or ""
        key = match.group("key")
        author, year = _parse_bibitem_label(label)
        lines.append(
            r"\bibcite{"
            + key
            + "}{{"
            + str(index)
            + "}{"
            + year
            + "}{{"
            + author
            + "}}{{}}}"
        )
    return lines


def _parse_bibitem_label(label: str) -> tuple[str, str]:
    cleaned = _latex_visible_text(label)
    year_match = re.search(r"\b(18|19|20)\d{2}\b", cleaned)
    year = year_match.group(0) if year_match is not None else ""
    author = cleaned.split("(", 1)[0].strip()
    if not author and cleaned:
        author = cleaned
    return author, year


def _extract_latex_caption(tex: str) -> str:
    match = re.search(r"\\caption(?:\s*\[[^\]]*\])?\s*\{", tex)
    if match is None:
        return ""
    return _balanced_brace_content(tex, match.end() - 1)


def _balanced_brace_content(text: str, open_brace_index: int) -> str:
    if open_brace_index < 0 or open_brace_index >= len(text) or text[open_brace_index] != "{":
        return ""
    depth = 0
    start = open_brace_index + 1
    index = open_brace_index
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
        index += 1
    return ""


def _latex_visible_text(text: str) -> str:
    previous = None
    cleaned = text
    while cleaned != previous:
        previous = cleaned
        cleaned = _LATEX_COMMAND_WITH_ARG_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"~", " ", cleaned)
    cleaned = re.sub(r"\\&", "&", cleaned)
    cleaned = _LATEX_COMMAND_RE.sub("", cleaned)
    cleaned = re.sub(r"[{}$]", " ", cleaned)
    return " ".join(cleaned.split())


def _find_main_tex(source_dir: Path) -> Path | None:
    preferred = source_dir / "main.tex"
    if preferred.is_file():
        return preferred
    for path in sorted(source_dir.rglob("*.tex")):
        payload = _read_file_bounded(path, max_bytes=100_000)
        if payload is None:
            continue
        sample = payload.decode("utf-8", errors="replace")
        if r"\documentclass" in sample and r"\begin{document}" in sample:
            return path
    return None


def _pdf_first_page_to_png(
    pdf_path: Path,
    *,
    scale: float,
    max_pixels: int = _MAX_RENDERED_PIXELS,
    max_png_bytes: int = _MAX_RENDERED_PNG_BYTES,
) -> bytes | None:
    try:
        import fitz
    except Exception:
        return None
    if not math.isfinite(scale) or scale <= 0:
        return None
    document = None
    try:
        document = fitz.open(pdf_path)
        if len(document) < 1:
            return None
        page = document[0]
        width = max(math.ceil(float(page.rect.width) * scale), 0)
        height = max(math.ceil(float(page.rect.height) * scale), 0)
        if width * height > max(max_pixels, 0):
            return None
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        if int(pixmap.width) * int(pixmap.height) > max(max_pixels, 0):
            return None
        png_bytes = pixmap.tobytes("png")
        return png_bytes if len(png_bytes) <= max(max_png_bytes, 0) else None
    except Exception:
        return None
    finally:
        if document is not None:
            document.close()


def _safe_extract_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
    *,
    budget: _ExtractionBudget | None = None,
) -> None:
    active_budget = budget or _ExtractionBudget()
    active_budget.observe_entry(member.size if member.isfile() else 0)
    if not member.isfile():
        return
    target = _safe_member_path(destination, member.name)
    if target is None:
        return
    source = archive.extractfile(member)
    if source is None:
        return
    with source:
        _copy_stream_bounded(
            source,
            target,
            budget=active_budget,
            max_bytes=active_budget.max_member_bytes,
        )


def _safe_extract_zip_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
    *,
    budget: _ExtractionBudget | None = None,
) -> None:
    active_budget = budget or _ExtractionBudget()
    active_budget.observe_entry(member.file_size if not member.is_dir() else 0)
    if member.is_dir():
        return
    target = _safe_member_path(destination, member.filename)
    if target is None:
        return
    with archive.open(member) as source:
        _copy_stream_bounded(
            source,
            target,
            budget=active_budget,
            max_bytes=active_budget.max_member_bytes,
        )


def _copy_stream_bounded(
    source: FigureBinaryReader,
    target: Path,
    *,
    budget: _ExtractionBudget,
    max_bytes: int,
) -> None:
    byte_limit = max(max_bytes, 0)
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as output:
            while True:
                remaining = byte_limit - written
                chunk = source.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError(f"arXiv source member exceeds {byte_limit} bytes")
                budget.consume(len(chunk))
                output.write(chunk)
                written += len(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise


class FigureBinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


def _read_file_bounded(path: Path, *, max_bytes: int) -> bytes | None:
    byte_limit = max(max_bytes, 0)
    try:
        if path.stat().st_size > byte_limit:
            return None
        with path.open("rb") as source:
            payload = source.read(byte_limit + 1)
    except OSError:
        return None
    return payload if len(payload) <= byte_limit else None


def _write_text_file_bounded(
    path: Path,
    text: str,
    *,
    max_bytes: int = _MAX_SOURCE_TEXT_BYTES,
) -> bool:
    payload = text.encode("utf-8")
    if len(payload) > max(max_bytes, 0):
        return False
    try:
        path.write_bytes(payload)
    except OSError:
        return False
    return True


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _response_content_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        value = int(str(raw).strip()) if raw is not None else -1
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _require_arxiv_response_url(response: object, *, fallback: str) -> None:
    geturl = getattr(response, "geturl", None)
    final_url = str(getattr(response, "url", None) or (geturl() if callable(geturl) else fallback))
    parsed = urllib.parse.urlsplit(final_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not (
        host == "arxiv.org" or host.endswith(".arxiv.org")
    ):
        raise ValueError(
            f"unsafe arXiv source redirect rejected: {fallback} -> {final_url}"
        )


def _restricted_tex_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["openin_any"] = "p"
    environment["openout_any"] = "p"
    return environment


def _safe_member_path(destination: Path, raw_name: str) -> Path | None:
    raw_name = raw_name.replace("\\", "/")
    if raw_name.startswith("/") or raw_name.startswith("../") or "/../" in raw_name:
        return None
    parts = [part for part in raw_name.split("/") if part]
    if not parts or any(part in {".", ".."} or ":" in part or "\x00" in part for part in parts):
        return None
    try:
        root = destination.resolve(strict=False)
        target = (root / Path(*parts)).resolve(strict=False)
        target.relative_to(root)
    except (OSError, ValueError):
        return None
    return target


def _relative_source_path(path: Path, source_dir: Path) -> str:
    try:
        return path.relative_to(source_dir).as_posix()
    except ValueError:
        return path.name


def _strip_latex_comment(line: str) -> str:
    escaped = False
    for index, char in enumerate(line):
        if char == "\\":
            escaped = not escaped
            continue
        if char == "%" and not escaped:
            return line[:index]
        escaped = False
    return line


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default

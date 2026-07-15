from __future__ import annotations

import gzip
import io
import sys
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

from zoteropdf2md.arxiv_source_recovery import (
    ArxivSourceRecoveryResult,
    LatexSourceFigureRenderer,
    SourceFigure,
    _expanded_tex_document,
    _pdf_first_page_to_png,
    _safe_member_path,
    collect_source_figures,
    extract_arxiv_source_package,
    fetch_arxiv_source_package,
    recover_latexml_figures_from_arxiv_source_html,
    _standalone_figure_document,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nz2m-recovered"


@pytest.fixture(autouse=True)
def _stub_safe_http_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zotero_metadata_enrichment.safe_http._resolve_target",
        lambda *_args, **_kwargs: (object(),),
    )


class FakeRenderer:
    def __init__(self) -> None:
        self.rendered: list[SourceFigure] = []

    def render(
        self,
        *,
        source_dir: Path,
        figure: SourceFigure,
        arxiv_id: str,
        index: int,
    ) -> bytes | None:
        del source_dir, arxiv_id, index
        self.rendered.append(figure)
        return PNG_BYTES


def test_collect_source_figures_keeps_single_file_setup_preamble(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    figures = source_dir / "figures"
    figures.mkdir(parents=True)
    (figures / "tree_taxonomy.tex").write_text(
        r"""
\tikzstyle{leaf}=[draw=blue]
\begin{figure*}
  \begin{forest}
    [Taxonomy [Branch, leaf]]
  \end{forest}
  \caption{The taxonomy of AI for research.}
  \label{fig:ai4research-taxonomy}
\end{figure*}
""",
        encoding="utf-8",
    )

    collected = collect_source_figures(source_dir)

    assert len(collected) == 1
    assert collected[0].relative_path == "figures/tree_taxonomy.tex"
    assert r"\tikzstyle{leaf}" in collected[0].tex
    assert collected[0].labels == ("fig:ai4research-taxonomy",)
    assert collected[0].environments == ("forest",)
    assert collected[0].caption_text == "The taxonomy of AI for research."


def test_standalone_figure_document_renders_float_body_without_float_environment() -> None:
    figure = SourceFigure(
        relative_path="figures/tree_taxonomy.tex",
        tex=r"""
\tikzstyle{leaf}=[draw=blue]
\begin{figure*}[!t]
  \centering
  \begin{forest}
    [Taxonomy [Branch, leaf]]
  \end{forest}
  \caption{The taxonomy of AI for research.}
\end{figure*}
""",
        caption_text="The taxonomy of AI for research.",
        labels=(),
        environments=("forest",),
    )

    document = _standalone_figure_document(source_dir=Path("."), main_tex=None, figure=figure)

    assert r"\tikzstyle{leaf}" in document
    assert r"\begin{figure" not in document
    assert r"\end{figure" not in document
    assert r"\begin{center}" in document
    assert r"\begin{forest}" in document


def test_recover_latexml_figure_from_arxiv_source_package() -> None:
    html = """
    <html><body>
      <main id="web-doc" data-z2m-source-kind="arxiv_latexml">
        <section id="S2">
          <figure class="ltx_figure" id="S2.F2">
            <div class="ltx_transformed_outer">
              <span class="ltx_ERROR undefined">{forest}</span>
              <p>raw forest source that LaTeXML failed to render</p>
            </div>
            <figcaption class="ltx_caption">
              Figure 2: The taxonomy of AI for research (AI4Research) is categorized into five key areas.
            </figcaption>
          </figure>
        </section>
      </main>
    </body></html>
    """
    renderer = FakeRenderer()

    result = recover_latexml_figures_from_arxiv_source_html(
        html,
        source_url="https://arxiv.org/html/2507.01903",
        fetch_source=lambda arxiv_id: _source_tarball(
            {
                "main.tex": r"""
\documentclass{article}
\usepackage[edges]{forest}
\definecolor{hidden-black}{RGB}{20,68,106}
\begin{document}
\input{figures/tree_taxonomy.tex}
\end{document}
""",
                "main.bbl": r"""
\begin{thebibliography}{1}
\bibitem[Kuhn et~al.(2022)Kuhn, Gal, and Farquhar]{kuhn2022clam}
A reference.
\end{thebibliography}
""",
                "figures/tree_taxonomy.tex": r"""
\tikzstyle{leaf}=[draw=hidden-black]
\begin{figure*}
  \begin{forest}
    [Taxonomy [Branch, leaf]]
  \end{forest}
  \caption{The taxonomy of AI for research (AI4Research) is categorized into five key areas.}
  \label{fig:ai4research-taxonomy}
\end{figure*}
""",
            }
        ),
        renderer=renderer,
    )

    assert isinstance(result, ArxivSourceRecoveryResult)
    assert result.recovered_figures == 1
    assert result.attempted_figures == 1
    assert renderer.rendered
    assert renderer.rendered[0].relative_path == "figures/tree_taxonomy.tex"
    assert "ltx_ERROR" not in result.html
    assert 'data-z2m-recovery="arxiv-source"' in result.html
    assert 'data-z2m-source-path="figures/tree_taxonomy.tex"' in result.html
    assert 'data-z2m-source-arxiv-id="2507.01903"' in result.html
    assert 'src="data:image/png;base64,' in result.html
    assert "Figure 2: The taxonomy of AI for research" in result.html


def test_recover_latexml_figure_is_noop_without_arxiv_id() -> None:
    html = '<figure><span class="ltx_ERROR undefined">{forest}</span></figure>'

    result = recover_latexml_figures_from_arxiv_source_html(html, source_url="https://example.test/article")

    assert result.html == html
    assert result.recovered_figures == 0
    assert result.attempted_figures == 0


def test_latex_source_renderer_can_use_docker_tex_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARXIV_SOURCE_RECOVERY_TEX_DOCKER_IMAGE", "ghcr.io/xu-cheng/texlive-full:latest")

    renderer = LatexSourceFigureRenderer()
    command = renderer._render_command(source_dir=tmp_path, standalone_name="figure.tex")

    assert command[:8] == [
        "docker",
        "run",
        "--rm",
        "-e",
        "openin_any=p",
        "-e",
        "openout_any=p",
        "-v",
    ]
    assert command[8].endswith(":/work")
    assert command[9:12] == ["-w", "/work", "ghcr.io/xu-cheng/texlive-full:latest"]
    assert command[-5:] == [
        "pdflatex",
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "figure.tex",
    ]


class _FetchResponse:
    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        self.body = body
        self.url = "https://arxiv.org/e-print/2507.01903"
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_sizes: list[int] = []

    def __enter__(self) -> "_FetchResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


def test_arxiv_source_fetch_rejects_declared_oversize_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FetchResponse(b"not-read", content_length=5)
    monkeypatch.setattr("zotero_metadata_enrichment.safe_http._open_pinned_once", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        fetch_arxiv_source_package("2507.01903", max_bytes=4)

    assert response.read_sizes == []


def test_arxiv_source_fetch_rejects_streamed_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FetchResponse(b"12345")
    monkeypatch.setattr("zotero_metadata_enrichment.safe_http._open_pinned_once", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        fetch_arxiv_source_package("2507.01903", max_bytes=4)

    assert response.read_sizes == [5]


def test_arxiv_source_fetch_rejects_cross_host_redirect_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FetchResponse(b"not-read")
    response.url = "http://127.0.0.1/private"
    monkeypatch.setattr("zotero_metadata_enrichment.safe_http._open_pinned_once", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="unsafe arXiv source redirect"):
        fetch_arxiv_source_package("2507.01903", max_bytes=100)

    assert response.read_sizes == []


def test_arxiv_source_blocks_cross_host_redirect_before_second_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    response = _FetchResponse(b"not-read")
    response.status = 302
    response.headers["Location"] = "https://other.example/private"

    def fake_open(request: object, **_kwargs: object) -> _FetchResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr("zotero_metadata_enrichment.safe_http._open_pinned_once", fake_open)

    with pytest.raises(ValueError, match="Redirect policy rejected"):
        fetch_arxiv_source_package("2507.01903", max_bytes=100)

    assert calls == ["https://arxiv.org/e-print/2507.01903"]
    assert response.read_sizes == []


def test_extract_tar_rejects_oversized_member_without_partial_file(tmp_path: Path) -> None:
    blob = _binary_tarball({"figures/large.tex": b"12345"})
    destination = tmp_path / "source"

    with pytest.raises(ValueError, match="member exceeds 4 bytes"):
        extract_arxiv_source_package(blob, destination, max_member_bytes=4)

    assert not (destination / "figures" / "large.tex").exists()


def test_extract_zip_enforces_total_budget(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.tex", b"123")
        archive.writestr("second.tex", b"456")
    destination = tmp_path / "source"

    with pytest.raises(ValueError, match="exceeds 5 extracted bytes"):
        extract_arxiv_source_package(
            buffer.getvalue(),
            destination,
            max_member_bytes=4,
            max_total_bytes=5,
        )

    assert (destination / "first.tex").read_bytes() == b"123"
    assert not (destination / "second.tex").exists()


def test_extract_gzip_rejects_expansion_bomb_without_partial_file(tmp_path: Path) -> None:
    blob = gzip.compress(b"12345")
    destination = tmp_path / "source"

    with pytest.raises(ValueError, match="member exceeds 4 bytes"):
        extract_arxiv_source_package(blob, destination, max_member_bytes=4)

    assert not (destination / "main.tex").exists()


def test_extract_archive_enforces_entry_budget(tmp_path: Path) -> None:
    blob = _binary_tarball({"first.tex": b"1", "second.tex": b"2"})
    destination = tmp_path / "source"

    with pytest.raises(ValueError, match="exceeds 1 entries"):
        extract_arxiv_source_package(blob, destination, max_entries=1)

    assert (destination / "first.tex").is_file()
    assert not (destination / "second.tex").exists()


def test_collect_source_figures_skips_oversized_tex(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "large.tex").write_text(
        r"\begin{figure}\caption{large}\end{figure}",
        encoding="utf-8",
    )

    assert collect_source_figures(source_dir, max_text_bytes=4) == []


def test_expanded_tex_document_rejects_input_outside_source_root(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    main_tex = source_dir / "main.tex"
    main_tex.write_text(r"before \input{../outside} after", encoding="utf-8")
    (tmp_path / "outside.tex").write_text("HOST SECRET", encoding="utf-8")

    expanded = _expanded_tex_document(main_tex, source_dir=source_dir)

    assert "HOST SECRET" not in expanded
    assert "before" in expanded and "after" in expanded


class _OversizedRenderer:
    def render(self, **_kwargs: object) -> bytes:
        return b"12345"


def test_recovery_rejects_oversized_external_renderer_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoteropdf2md import arxiv_source_recovery as recovery_module

    monkeypatch.setattr(recovery_module, "_MAX_RENDERED_PNG_BYTES", 4)
    html = '<figure id="F1"><span class="ltx_ERROR">forest</span></figure>'
    source = _source_tarball(
        {
            "main.tex": (
                r"\begin{figure}\begin{forest}[Tree]\end{forest}"
                r"\caption{Tree}\end{figure}"
            )
        }
    )

    result = recover_latexml_figures_from_arxiv_source_html(
        html,
        source_url="https://arxiv.org/html/2507.01903",
        fetch_source=lambda _arxiv_id: source,
        renderer=_OversizedRenderer(),
    )

    assert result.recovered_figures == 0
    assert any(error.startswith("render_too_large:F1:") for error in result.errors)
    assert "data:image/png" not in result.html


def test_recovery_rejects_oversized_injected_source_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoteropdf2md import arxiv_source_recovery as recovery_module

    monkeypatch.setattr(recovery_module, "_MAX_SOURCE_PACKAGE_BYTES", 4)
    html = '<figure id="F1"><span class="ltx_ERROR">forest</span></figure>'

    result = recover_latexml_figures_from_arxiv_source_html(
        html,
        source_url="https://arxiv.org/html/2507.01903",
        fetch_source=lambda _arxiv_id: b"12345",
        renderer=FakeRenderer(),
    )

    assert result.recovered_figures == 0
    assert result.errors == ("source_recovery_error:arXiv source package exceeds 4 bytes",)


def test_pdf_render_rejects_excessive_pixel_count_before_pixmap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        rect = SimpleNamespace(width=100, height=100)

        def get_pixmap(self, **_kwargs: object) -> object:
            raise AssertionError("pixmap must not be allocated")

    class FakeDocument:
        closed = False

        def __len__(self) -> int:
            return 1

        def __getitem__(self, _index: int) -> FakePage:
            return FakePage()

        def close(self) -> None:
            self.closed = True

    document = FakeDocument()
    fitz = ModuleType("fitz")
    fitz.open = lambda _path: document  # type: ignore[attr-defined]
    fitz.Matrix = lambda x, y: (x, y)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", fitz)

    result = _pdf_first_page_to_png(
        tmp_path / "figure.pdf",
        scale=1.0,
        max_pixels=9_999,
    )

    assert result is None
    assert document.closed is True


def test_pdf_render_rejects_oversized_encoded_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePixmap:
        width = 1
        height = 1

        def tobytes(self, _format: str) -> bytes:
            return b"12345"

    class FakePage:
        rect = SimpleNamespace(width=1, height=1)

        def get_pixmap(self, **_kwargs: object) -> FakePixmap:
            return FakePixmap()

    class FakeDocument:
        closed = False

        def __len__(self) -> int:
            return 1

        def __getitem__(self, _index: int) -> FakePage:
            return FakePage()

        def close(self) -> None:
            self.closed = True

    document = FakeDocument()
    fitz = ModuleType("fitz")
    fitz.open = lambda _path: document  # type: ignore[attr-defined]
    fitz.Matrix = lambda x, y: (x, y)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", fitz)

    result = _pdf_first_page_to_png(
        tmp_path / "figure.pdf",
        scale=1.0,
        max_png_bytes=4,
    )

    assert result is None
    assert document.closed is True


def test_archive_member_path_rejects_windows_drive_and_ads_names(tmp_path: Path) -> None:
    assert _safe_member_path(tmp_path, "C:/escape.tex") is None
    assert _safe_member_path(tmp_path, "safe.tex:stream") is None
    assert _safe_member_path(tmp_path, "../escape.tex") is None
    assert _safe_member_path(tmp_path, "safe/../../escape.tex") is None
    assert _safe_member_path(tmp_path, "bad\x00name.tex") is None
    assert _safe_member_path(tmp_path, "safe/figure.tex") == tmp_path / "safe" / "figure.tex"


def _source_tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _binary_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from zoteropdf2md.arxiv_source_recovery import (
    ArxivSourceRecoveryResult,
    SourceFigure,
    collect_source_figures,
    recover_latexml_figures_from_arxiv_source_html,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nz2m-recovered"


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


def _source_tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()

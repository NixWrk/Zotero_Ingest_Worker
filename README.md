# Zotero Ingest / Full-Text Workers

Small services for the first Zotero pipeline stages.

- `zotero-ingest-worker` runs in `metadata` role and enriches Zotero parent
  metadata only.
- `zotero-fulltext-worker` runs in `fulltext` role and handles native full-text
  acquisition: publisher/PMC/IOP/arXiv HTML, PDF download, ResearchGate browser
  PDF fallback, and Sci-Hub as the last PDF fallback.

The full-text worker searches by all identifiers it can derive from Zotero and
metadata enrichment: DOI, PMID, PMCID, arXiv id, URL, and title evidence. A
successful PDF/HTML attachment is reported back through `downstream_orchestrator`
so the main Zotero worker can enqueue later OCR, PDF-to-HTML, polish, or
translation stages without duplicating full-text provider logic.

The code currently lives in one repository so both containers share Zotero
SQLite access, queue storage, provider configuration, and tests. The HTTP
surface is role-guarded, so the metadata container does not expose full-text
download endpoints.

Neither worker runs OCR, PDF-to-HTML conversion, or translation. When a newly
attached PDF/HTML needs a downstream stage, the full-text worker returns a
`downstream_orchestrator` reference with the new Zotero attachment key and local
path. The main orchestrator is responsible for dispatching those later stages to
smaller containers.

## Article HTML Standard

Accepted native HTML downloads are normalized into an `article-html-standard/v1`
package before Zotero attachment:

```text
article.html
assets/
manifest.json
quality.json
source/
logs/
```

`manifest.json` records title/authors/identifiers, source provider and URL,
normalizer version, asset list, and quality status. `quality.json` checks title,
text length, local images, internal links, bibliography anchors, math strategy,
remote assets, and provenance. The Zotero HTML attachment uses the standardized
`article.html`; local assets are embedded into the attachment copy.

The same image also exposes `zotero-article-polish`, which applies the shared
web article-standard polish to existing source HTML before the main Zotero
worker sends it to language detection or translation:

```powershell
zotero-article-polish input.html output.html
```

The main Zotero worker runs that CLI from `zotero-ingest-worker:latest` with the
ingest repository mounted as `/ingest_worker_repo`.

Provider-specific polish entrypoints are also installed for manual testing and
targeted repair:

```powershell
pdf-html-polish-web input.html output.html
pdf-html-polish-web-arxiv input.html output.html
pdf-html-polish-web-pmc input.html output.html
pdf-html-polish-web-iop input.html output.html
pdf-html-polish-web-springer-nature input.html output.html
```

## arXiv Source Recovery

Official arXiv HTML can contain failed LaTeXML figure bodies even when the
article text is otherwise good. During arXiv web polish, the worker can recover
those broken figures from the original arXiv source package:

1. detect broken LaTeXML figure fragments in polished arXiv HTML;
2. fetch `https://arxiv.org/e-print/<arxiv_id>`;
3. extract the source package safely;
4. collect matching LaTeX figure environments;
5. render selected figures to PNG through local TeX and PyMuPDF;
6. replace the broken HTML figure body with a standard embedded recovered image.

This is enabled by default for arXiv polish and can be controlled with:

```text
ARXIV_SOURCE_RECOVERY_ENABLED=1
ARXIV_SOURCE_RECOVERY_MAX_FIGURES=4
ARXIV_SOURCE_RECOVERY_FETCH_TIMEOUT_SECONDS=60
ARXIV_SOURCE_RECOVERY_TIMEOUT_SECONDS=120
ARXIV_SOURCE_RECOVERY_TEX_COMMAND=pdflatex
```

The Docker image installs the required TeX Live packages and `PyMuPDF`. CLI
stdout reports `source_figures=<recovered>/<attempted>` and
`source_figure_errors=...` so audits can distinguish publisher-source defects
from recovery defects.

## Source HTML Quality Audit

`zotero-source-html-audit` checks attached source HTML files in Zotero storage
against the article-standard shell: polish style marker, `#web-doc`, source
kind, local fragment links, image sources, tables, script tags, duplicate source
attachments, orphan files, and source HTML job history. It also includes
regression gates for known web-polish failures: generic embedded image MIME
types, `<picture>` source overrides of embedded images, LaTeXML `\rowcolor`
artifacts, missing table/caption/formula/abbreviation CSS rules, and stale
active `[ARXIV HTML]` siblings when a parent already has `[SOURCE HTML]`.

```powershell
zotero-source-html-audit `
  --zotero-root C:\PC\Zotero `
  --zotero-root C:\Users\ELVIS_NIX\Zotero `
  --state-db D:\Elvis_projects\Zotero_Automation\Zotero_automatization\data\ocr\state.sqlite `
  --pretty
```

By default the command writes timestamped and latest JSON reports under
`INGEST_HTML_ROOT/diagnostics`. `critical_records` means the current HTML file
does not satisfy the standard. `warning_counts` keeps softer process signals,
including missing historic source-html job success rows and figures whose media
was absent in the publisher source.

`zotero-fulltext-worker source-html-cleanup` repairs Zotero attachment records
that would otherwise produce duplicate `[source HTML]` rows in the UI. It scans
parent items, finds source HTML attachments whose local file is missing, and
deduplicates multiple valid source HTML attachments by keeping the largest/newest
one. The command is dry-run by default and only targets source HTML attachments;
PDFs, generated translation HTML, and parent items are outside its scope.

```powershell
zotero-fulltext-worker source-html-cleanup --limit 1000
zotero-fulltext-worker source-html-cleanup --limit 1000 --apply --confirm
```

The full-text attach path runs the same cleanup before creating a new source
HTML. If a valid source HTML is already attached, the worker skips creating a
duplicate and can still attach a newly found PDF for the same parent item.

## Main Commands

```powershell
zotero-ingest-worker serve
zotero-fulltext-worker serve
zotero-ingest-worker metadata-backlog-scan --limit 100
zotero-ingest-worker metadata-drain-queue --limit 32
zotero-ingest-worker arxiv-html-backlog-scan --limit 100
zotero-ingest-worker arxiv-html-drain-queue --limit 32
zotero-fulltext-worker full-text-backlog-scan --limit 100
zotero-fulltext-worker full-text-drain-queue --limit 32
zotero-fulltext-worker scihub-pdf-backlog-scan --limit 100
zotero-fulltext-worker scihub-pdf-drain-queue --limit 32
zotero-fulltext-worker source-html-cleanup --limit 1000
```

## Container

```powershell
docker compose up -d zotero-ingest-worker zotero-fulltext-worker
```

The default image is `zotero-ingest-worker:latest`.

- metadata worker: `127.0.0.1:8767`
- full-text worker: `127.0.0.1:8766`

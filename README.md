# Zotero Ingest / Full-Text Workers

Small services for the first Zotero pipeline stages.

- `zotero-ingest-worker` runs in `metadata` role and enriches Zotero parent
  metadata only.
- `zotero-fulltext-worker` runs in `fulltext` role and handles native full-text
  acquisition: publisher/PMC/IOP/arXiv HTML, PDF download, ResearchGate browser
  PDF fallback, and Sci-Hub as the last PDF fallback.

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

## Source HTML Quality Audit

`zotero-source-html-audit` checks attached source HTML files in Zotero storage
against the article-standard shell: polish style marker, `#web-doc`, source
kind, local fragment links, image sources, tables, script tags, duplicate source
attachments, orphan files, and source HTML job history.

```powershell
zotero-source-html-audit `
  --zotero-root C:\PC\Zotero `
  --zotero-root C:\Users\ELVIS_NIX\Zotero `
  --state-db D:\Elvis_projects\Zotero_automatization\data\ocr\state.sqlite `
  --pretty
```

By default the command writes timestamped and latest JSON reports under
`INGEST_HTML_ROOT/diagnostics`. `critical_records` means the current HTML file
does not satisfy the standard. `warning_counts` keeps softer process signals,
including missing historic source-html job success rows and figures whose media
was absent in the publisher source.

## Main Commands

```powershell
zotero-ingest-worker serve
zotero-fulltext-worker serve
zotero-ingest-worker metadata-backlog-scan --limit 100
zotero-ingest-worker metadata-drain-queue --limit 32
zotero-fulltext-worker full-text-backlog-scan --limit 100
zotero-fulltext-worker full-text-drain-queue --limit 32
zotero-fulltext-worker scihub-pdf-backlog-scan --limit 100
zotero-fulltext-worker scihub-pdf-drain-queue --limit 32
```

## Container

```powershell
docker compose up -d zotero-ingest-worker zotero-fulltext-worker
```

The default image is `zotero-ingest-worker:latest`.

- metadata worker: `127.0.0.1:8767`
- full-text worker: `127.0.0.1:8766`

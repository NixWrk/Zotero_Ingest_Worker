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

- metadata worker: `127.0.0.1:8765`
- full-text worker: `127.0.0.1:8766`

# Zotero Ingest Worker

Small service for the first Zotero pipeline stage: enrich metadata, discover source HTML, discover/download PDF attachments, and attach those files back to Zotero.

It deliberately does not run OCR, PDF-to-HTML conversion, or translation. When a newly attached PDF/HTML needs a downstream stage, the worker returns a `downstream_orchestrator` reference with the new Zotero attachment key and local path. The main orchestrator is responsible for dispatching those later stages to smaller containers.

## Main Commands

```powershell
zotero-ingest-worker serve
zotero-ingest-worker metadata-backlog-scan --limit 100
zotero-ingest-worker metadata-drain-queue --limit 32
zotero-ingest-worker full-text-backlog-scan --limit 100
zotero-ingest-worker full-text-drain-queue --limit 32
zotero-ingest-worker scihub-pdf-backlog-scan --limit 100
zotero-ingest-worker scihub-pdf-drain-queue --limit 32
zotero-ingest-worker full-run-start --drain-limit 32
```

## Container

```powershell
docker compose up -d zotero-ingest-worker
```

The default container image is `zotero-ingest-worker:latest`, serving HTTP on `127.0.0.1:8765`.

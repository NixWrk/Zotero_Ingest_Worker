# Nested worker packages

The ingest worker image builds against two local package directories under this
folder. They are source packages installed into the image during build, so a
clean checkout must keep both directories present.

Required checkouts for the current main-worker build:

- `repositories/zotero-metadata-enrichment`
  - purpose: metadata lookup/diff logic using Zotero Translation Server,
    Crossref, and arXiv.
- `repositories/zotero-arxiv-html-ingest`
  - purpose: arXiv identifier lookup, HTML fetch/validation, artifact storage,
    and optional Zotero sibling attachment relay.

`Dockerfile` copies and installs both directories. A clean clone of this
repository needs these checkouts restored before building
`zotero-ingest-worker:latest`.

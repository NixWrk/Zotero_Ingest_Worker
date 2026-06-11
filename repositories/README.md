# Nested worker packages

The main worker image builds against two local package repositories under this
directory. They are intentionally separate git repositories, not vendored source
inside the main repository.

Required checkouts for the current main-worker build:

- `repositories/zotero-metadata-enrichment`
  - commit: `c7a67b9fcf0fc8c9b0ba5b77b13bdf07d844a22b`
  - purpose: metadata lookup/diff logic using Zotero Translation Server,
    Crossref, and arXiv.
- `repositories/zotero-arxiv-html-ingest`
  - commit: `121c066e8a81ea06c7b59b854773319fadbefbf4`
  - purpose: arXiv identifier lookup, HTML fetch/validation, artifact storage,
    and optional Zotero sibling attachment relay.

`Dockerfile.zotero-worker` copies and installs both directories. A clean clone of
the main repository needs these checkouts restored before building
`zotero-worker:latest`.

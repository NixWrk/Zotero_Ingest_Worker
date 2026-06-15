# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ZOTERO_INGEST_HOST=0.0.0.0
ENV ZOTERO_INGEST_PORT=8765
ENV ZOTERO_INGEST_ROLE=metadata

WORKDIR /app

COPY pyproject.toml README.md ./

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install \
        "cryptography>=3.1" \
        "playwright>=1.45" \
        "pypdf>=4.0"

RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    python -m playwright install --with-deps chromium

COPY repositories/zotero-metadata-enrichment ./repositories/zotero-metadata-enrichment
COPY repositories/zotero-arxiv-html-ingest ./repositories/zotero-arxiv-html-ingest

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps \
        ./repositories/zotero-metadata-enrichment \
        ./repositories/zotero-arxiv-html-ingest

COPY zotero_ingest_worker ./zotero_ingest_worker
COPY scripts ./scripts

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

EXPOSE 8765

CMD ["zotero-ingest-worker", "serve", "--host", "0.0.0.0", "--port", "8765"]

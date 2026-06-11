# Architecture

## Контур данных

```text
Zotero local metadata
  -> arXiv evidence extraction
  -> arXiv API lookup
  -> confidence gate
  -> arxiv.org/html/{id}
  -> local html artifact
  -> zotero-file-relay
  -> Zotero sibling attachment
```

## Очередь

Таблица: `metadata_jobs`

Для этого блока:

```text
job_type = arxiv_html
```

Ключевые поля:

- `library_id`
- `attachment_key`
- `source_path`
- `source_size`
- `source_mtime_ns`
- `parent_item_key`
- `status`
- `output_path`
- `result_json`
- `relay_result`

## Evidence Sources

Порядок поиска arXiv ID:

1. Exact ID из metadata:
   - `arXiv:2401.01234`
   - `https://arxiv.org/abs/2401.01234`
   - `https://arxiv.org/pdf/2401.01234.pdf`
2. DOI:
   - `10.48550/arXiv.2401.01234`
3. Relations:
   - Zotero relation object с arXiv URL.
4. Result от Zotero translators:
   - `archive`
   - `archiveLocation`
   - `extra`
   - `url`
5. Title search в arXiv API.

## Confidence Gate

Exact arXiv ID:

- score = `1.0`
- можно обрабатывать автоматически.

Title search:

- требует score >= `ARXIV_SEARCH_MIN_SCORE`
- желательно учитывать authors/year после доработки.

Низкая уверенность:

- job переводится в `skipped` или `manual_review` после добавления статуса review.

## HTML Fetch

Primary endpoint:

```text
https://arxiv.org/html/{arxiv_id}
```

Текущий v1 не использует `ar5iv` как замену, потому что цель - официальный arXiv HTML. Возможный fallback можно добавить отдельным opt-in флагом.

## Artifact Layout

```text
data/html/arxiv/
  <library_id>/
    <attachment_key>/
      <source_size>_<source_mtime_ns>/
        <safe_document_stem>/
          <safe_document_stem> [ARXIV HTML].html
          manifest.json
```

`manifest.json` должен содержать:

- `library_id`
- `attachment_key`
- `source_pdf`
- `arxiv_id`
- `html_url`
- `candidate`
- `provider`
- `confidence`
- `output`
- `created_at`

## Zotero Attachment

Relay endpoint:

```text
POST /attachments/{pdfKey}/siblings/html
```

Filename:

```text
<document_name> [ARXIV HTML].html
```

Dedup key должен включать:

- library id
- attachment key
- arXiv id
- source HTML mtime/signature


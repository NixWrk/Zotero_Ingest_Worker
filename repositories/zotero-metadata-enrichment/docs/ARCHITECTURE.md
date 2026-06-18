# Architecture

## Контур данных

```text
Zotero local sqlite/storage
  -> zotero-ingest-worker metadata scanner
  -> metadata_jobs queue
  -> provider chain
       1. Zotero translation-server
       2. Crossref
       3. arXiv API
  -> normalized candidate
  -> diff/policy engine
  -> zotero-file-relay
  -> Zotero Web API
```

## Очередь

Таблица: `metadata_jobs`

Ключевые поля:

- `job_type`: `enrich`
- `library_id`
- `attachment_key`
- `parent_item_key`
- `parent_version`
- `queue_key`
- `status`
- `result_json`
- `relay_result`

`queue_key` должен меняться при изменении provider/policy/threshold, чтобы можно было явно переобработать документы после изменения логики.

## Provider Chain

### 1. Zotero Translation Server

Используется первым, потому что это наиболее близко к тому, как metadata находит сам Zotero.

Endpoints:

- `POST /search`
- `POST /web`

Источники для `/search`:

- DOI
- ISBN
- PMID
- arXiv ID

Источники для `/web`:

- URL из Zotero metadata
- DOI URL, например `https://doi.org/...`
- arXiv URL

### 2. Crossref

Используется для DOI lookup и title search, когда Zotero translators ничего уверенного не вернули.

### 3. arXiv API

Используется для arXiv ID lookup и title search.

## Normalized Candidate

Candidate должен иметь стабильную структуру:

```json
{
  "source": "zotero_translation_server_search",
  "identifier": "10.1000/example",
  "score": 1.0,
  "fields": {
    "title": "...",
    "DOI": "...",
    "abstractNote": "...",
    "date": "...",
    "url": "..."
  },
  "extended": {
    "creators": [],
    "tags": [],
    "relations": []
  },
  "raw": {}
}
```

В текущем worker `extended` пока хранится в `raw`, потому что relay еще не умеет безопасно писать эти поля.

## Policy Engine

Режимы:

- `dry-run`: не писать в Zotero, только сохранять diff.
- `emptyFieldsOnly`: дополнять только пустые поля.
- `allowOverwrite`: разрешить замену существующих scalar-полей.

Для массового запуска дефолт: `emptyFieldsOnly`.

## Relay Boundary

Текущий endpoint:

```text
PATCH /attachments/{pdfKey}/parent/metadata
```

Он безопасно патчит scalar allowlist. Запрещенные поля:

- `creators`
- `tags`
- `relations`
- `collections`
- structural fields

Для полноценного enrichment нужен новый extended endpoint.

# Operations

## Dry-run queue

```powershell
zotero-ingest-worker arxiv-html-backlog-scan --limit 20
zotero-ingest-worker arxiv-html-drain-queue --dry-run --limit 20
zotero-ingest-worker metadata-queue --type arxiv_html --limit 20
```

## Apply one job

```powershell
zotero-ingest-worker arxiv-html-drain-queue --limit 1
```

## Full-run integration

Payload flags:

```json
{
  "arxiv_html_backlog_intake": true,
  "arxiv_html_drain": true,
  "require_relay": true,
  "dry_run": false
}
```

## Где искать результат

В контейнере:

```text
/data/html/arxiv/...
```

На Windows host:

```text
D:\Elvis_projects\zotero\Zotero_automatization\data\html\arxiv\...
```

В Zotero:

```text
<PDF parent item>
  <original pdf>
  <document_name> [ARXIV HTML].html
```

## Проверки перед массовым запуском

1. `arxiv-html-backlog-scan --limit 20`
2. Проверить queued jobs.
3. `arxiv-html-drain-queue --dry-run --limit 20`
4. Запустить 1 реальный job.
5. Открыть HTML локально.
6. Проверить sibling attachment в Zotero.
7. Проверить Zotero/WebDAV sync.

## Типовые ошибки

### `arxiv_html_404`

arXiv record найден, но официальный HTML недоступен.

Действие: skip или future fallback.

### `low_confidence`

Title search не прошел threshold.

Действие: manual review.

### `relay failed`

HTML сохранен локально, но не прикрепился в Zotero.

Действие: retry job после проверки `zotero-file-relay`.

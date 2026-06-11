# Operations

## Запуск translation-server

```powershell
docker compose -f docker-compose.zotero-translation-server.yml up -d
```

Worker должен видеть его по Docker DNS:

```env
ZOTERO_TRANSLATION_SERVER_URL=http://zotero-translation-server:1969
```

Локальная проверка:

```powershell
docker ps
docker logs zotero-translation-server --tail 50
```

## Dry-run enrichment

```powershell
python -m zotero_ingest_worker.__main__ metadata-backlog-scan --limit 20
python -m zotero_ingest_worker.__main__ metadata-drain-queue --dry-run --limit 20
python -m zotero_ingest_worker.__main__ metadata-queue --type enrich --limit 20
```

## Conservative apply

```powershell
python -m zotero_ingest_worker.__main__ metadata-drain-queue --limit 5 --policy emptyFieldsOnly
```

## Overwrite mode

Использовать только после просмотра diff-отчета.

```powershell
python -m zotero_ingest_worker.__main__ metadata-drain-queue --limit 1 --policy allowOverwrite
```

## Full-run integration

Payload flags:

```json
{
  "metadata_backlog_intake": true,
  "metadata_drain": true,
  "require_relay": true,
  "dry_run": false
}
```

## Наблюдение

Проверять:

- `metadata_jobs.status`
- `result_json.candidate.source`
- `result_json.patch`
- `relay_result.appliedFields`
- `relay_result.skippedFields`

Типовые статусы:

- `queued`
- `running`
- `succeeded`
- `skipped`
- `failed_retryable`
- `failed_final`
- `cancelled`


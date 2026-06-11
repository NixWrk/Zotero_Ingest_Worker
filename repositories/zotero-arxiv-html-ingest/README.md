# Zotero arXiv HTML Ingest

Отдельный проектный репозиторий для блока поиска arXiv-версий статей, загрузки HTML с arXiv и записи результата в Zotero/storage.

Цель: если в metadata статьи есть arXiv-ссылка или статью можно уверенно найти на arXiv, получить официальный HTML с `arxiv.org/html/{id}`, сохранить его в локальное хранилище и прикрепить как sibling attachment к PDF.

Репозиторий уже является рабочим Python-пакетом: его можно тестировать, импортировать как библиотеку и запускать CLI для lookup/fetch/validate.

## Основная идея

1. Worker берет PDF attachment и parent metadata.
2. Ищет arXiv ID:
   - DOI `10.48550/arXiv...`
   - `extra`
   - `url`
   - `relations`
   - metadata, найденные Zotero translators
   - arXiv API title search
3. Проверяет confidence.
4. Запрашивает:

```text
https://arxiv.org/html/{arxiv_id}
```

5. Сохраняет HTML:

```text
data/html/arxiv/
  <library_id>/
    <attachment_key>/
      <size>_<mtime>/
        <document_name>/
          <document_name> [ARXIV HTML].html
          manifest.json
```

6. Через `zotero-file-relay` прикрепляет HTML как sibling attachment:

```text
<document_name> [ARXIV HTML].html
```

## Связанные сервисы

- `zotero-worker` - очередь и обработчик `arxiv_html`.
- `zotero-file-relay` - запись HTML sibling в Zotero/WebDAV.
- `zotero/translation-server` - optional metadata evidence для нахождения arXiv ID.
- `arxiv.org/api/query` - поиск/проверка arXiv records.

## Минимальная конфигурация

```env
ARXIV_HTML_ROOT=/data/html/arxiv
ARXIV_HTML_ATTACH=1
ARXIV_HTML_FETCH_TIMEOUT_SECONDS=120
ARXIV_SEARCH_MIN_SCORE=0.88
ZOTERO_RELAY_URL=http://zotero-file-relay:23119
```

## Локальная разработка

```powershell
python -m pytest -q
$env:PYTHONPATH='src'; python -m zotero_arxiv_html_ingest.cli --help
```

Package layout:

```text
src/zotero_arxiv_html_ingest/
  html_fetch.py
  identifiers.py
  ingest.py
  lookup.py
  models.py
  relay.py
  storage.py
```

CLI:

```powershell
$env:PYTHONPATH='src'
python -m zotero_arxiv_html_ingest.cli lookup --arxiv-id 2401.01234
python -m zotero_arxiv_html_ingest.cli validate article.html
python -m zotero_arxiv_html_ingest.cli fetch --arxiv-id 2401.01234 --output-root D:\...\data\html\arxiv --library-id LIB --attachment-key PDFKEY --source-pdf D:\...\paper.pdf
```

## Команды интеграции в текущем worker

```powershell
python -m zotero_ingest_worker.__main__ arxiv-html-backlog-scan --limit 10
python -m zotero_ingest_worker.__main__ arxiv-html-drain-queue --dry-run --limit 5
python -m zotero_ingest_worker.__main__ arxiv-html-drain-queue --limit 1
python -m zotero_ingest_worker.__main__ metadata-queue --type arxiv_html
```

## Статус

Пакет и текущая интеграция в worker уже умеют:

- извлекать arXiv ID из DOI/URL/extra;
- искать arXiv по title через arXiv API;
- сохранять HTML в `data/html/arxiv`;
- прикреплять sibling через relay;
- писать manifest рядом с HTML.

Главная следующая доработка: сделать audit/render validation, чтобы не прикреплять пустые или некачественные HTML-страницы.

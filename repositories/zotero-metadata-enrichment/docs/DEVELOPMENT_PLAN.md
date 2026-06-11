# Development Plan

## Phase 1 - Stabilize Provider Chain

Цель: сделать Zotero translators основным и наблюдаемым provider.

Работы:

- добавить healthcheck translation-server в worker;
- явно логировать provider availability;
- сохранять translator provenance;
- различать `no_identifier`, `provider_unavailable`, `no_match`, `low_confidence`;
- добавить retry/backoff для 429/5xx.

Готово, когда:

- dry-run на 100 статьях показывает provider breakdown;
- ошибки translation-server не ломают весь drain;
- Crossref/arXiv fallback срабатывает только после Zotero translators.

## Phase 2 - Diff Report

Цель: перед записью видеть полный отчет изменений.

Работы:

- сохранять `current`, `candidate`, `patch`, `skipped_fields`;
- добавить файл-экспорт JSONL/CSV;
- добавить confidence reason;
- добавить manual review status для конфликтных случаев.

Готово, когда:

- можно открыть один отчет и понять, почему поле будет изменено;
- `allowOverwrite` не применяется без видимого diff.

## Phase 3 - Extended Metadata Relay

Цель: писать creators/tags/relations безопасно.

Работы в `zotero-file-relay`:

- добавить endpoint `PATCH /attachments/{pdfKey}/parent/metadata-extended`;
- поддержать `creators` с полной заменой или merge-policy;
- поддержать `tags` merge-only;
- поддержать `relations` merge-only;
- оставить `collections` вне v1 или сделать только explicit opt-in;
- покрыть optimistic locking по parent item version.

Готово, когда:

- Zotero item получает авторов из translators;
- повторный вызов идемпотентен;
- конфликт версии возвращает понятный `VERSION_CONFLICT`.

## Phase 4 - Matching Quality

Цель: снизить риск неправильного enrichment.

Работы:

- title+author scoring;
- DOI exact match всегда выше title match;
- arXiv ID exact match всегда выше title match;
- reject candidates с другим годом/журналом при низком score;
- manual review для неоднозначных результатов.

Готово, когда:

- на тестовой выборке нет очевидных false positives;
- низкоуверенные matches не пишутся автоматически.

## Phase 5 - Monitoring and UI

Цель: видеть процесс в worker GUI/API.

Работы:

- добавить metadata queue в GUI;
- добавить provider breakdown;
- добавить кнопки dry-run/apply/retry/cancel;
- добавить ссылку на diff для job.

Готово, когда:

- можно управлять enrichment без CLI;
- status page показывает running/queued/succeeded/skipped.

## Phase 6 - Rollout

Порядок:

1. Dry-run на 20 документах Meine.
2. Dry-run на 20 документах Heart and Lung.
3. Dry-run на 20 документах Elvis.
4. Apply `emptyFieldsOnly`.
5. Проверка Zotero sync.
6. Extended metadata после доработки relay.
7. `allowOverwrite` только для ручной выборки.

## Phase 7 - HTML Full-Text Source Coverage (publisher profiles)

Цель: расширять список издателей, у которых discovery умеет забирать полнотекстовый
HTML (и derived PDF) в `html_sources.py` (`looks_like_full_article_url`,
`canonical_article_html_url`, `html_profile_for_location`).

Backlog издателей:

- **IOP Science** (`iopscience.iop.org`): страница статьи `/article/<DOI>` отдаёт
  полнотекстовый HTML со статьёй И ссылку на PDF (`/article/<DOI>/pdf`). Сейчас
  HTML не скачивается (landing не распознаётся как full-article), PDF — берётся.
  Доработка: добавить IOP в `looks_like_full_article_url`, чтобы `/article/<DOI>`
  трактовался как `html`, и при необходимости дерайвить PDF из `/article/<DOI>/pdf`.
  Пример: `https://iopscience.iop.org/article/10.1088/1741-2552/ade918`.
  Реализовано: IOP article pages распознаются как full-article HTML, а PDF
  дерайвится из article URL.

- **PMC PDF из HTML-страницы** (`pmc.ncbi.nlm.nih.gov/articles/PMC<ID>/`): у страницы
  статьи кроме полнотекстового HTML есть и PDF — его публикует мета-тег
  `<meta name="citation_pdf_url" content=".../articles/PMC<ID>/pdf/<file>.pdf">`
  (плюс относительный `href="pdf/<file>.pdf"`). Именно его находит Zotero web
  connector. Сейчас PDF из PMC-HTML не дерайвится. Доработка: при PMC-HTML локации
  читать `citation_pdf_url` и добавлять PDF-локацию (игнорируя `*-supplement*.pdf`).
  Лучше сделать обобщённо — поддержать `citation_pdf_url` как источник PDF для любой
  landing/HTML-страницы (помогает PMC, IOP и многим издателям).
  Пример: `https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/` →
  `https://pmc.ncbi.nlm.nih.gov/articles/PMC12013345/pdf/nihms-2072483.pdf`.
  Реализовано: `citation_pdf_url` собирается из HTML и передаётся в PDF downloader
  как derived PDF location.

Готово, когда:

- для IOP- и PMC-статьи discovery скачивает и source HTML, и PDF;
- `citation_pdf_url` используется как общий сигнал для деривации PDF из landing-страниц;
- добавление нового издателя сводится к одному профилю + тесту на реальном URL.

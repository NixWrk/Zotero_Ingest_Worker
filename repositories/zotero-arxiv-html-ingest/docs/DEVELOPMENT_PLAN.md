# Development Plan

## Phase 1 - Evidence Extraction

Цель: находить arXiv ID максимально надежно.

Работы:

- извлекать ID из DOI, URL, `extra`, relations;
- использовать Zotero translators result как evidence;
- нормализовать old-style IDs (`cs/9901001`) и new-style IDs (`2401.01234`);
- сохранять evidence trail в manifest/job result.

Готово, когда:

- exact ID matches не требуют title search;
- manifest показывает, откуда взялся arXiv ID.

## Phase 2 - Search Quality

Цель: безопасно находить статьи без прямой arXiv ссылки.

Работы:

- title score;
- author overlap score;
- year proximity;
- reject при конфликте DOI/title/year;
- manual review для неоднозначных matches.

Готово, когда:

- title-only search не создает очевидных false positives;
- низкие score не прикрепляются автоматически.

## Phase 3 - HTML Validation

Цель: не сохранять пустой или сломанный HTML.

Работы:

- проверить HTTP status и content-type;
- проверить наличие `<html>`;
- проверить минимальный текстовый объем;
- сохранить diagnostics;
- добавить optional Playwright render smoke test;
- добавить screenshot thumbnail в diagnostics.

Готово, когда:

- пустая страница не прикрепляется в Zotero;
- job result объясняет причину skip/fail.

## Phase 4 - Attachment Deduplication

Цель: не плодить одинаковые arXiv HTML siblings.

Работы:

- инвентаризация existing HTML siblings;
- считать `[ARXIV HTML]` generated attachment;
- dedup по arXiv ID и filename;
- replace/update mode как отдельный opt-in.

Готово, когда:

- повторный drain не создает дубликат;
- retry идемпотентен.

## Phase 5 - Storage/Export

Цель: сделать HTML артефакты удобными для проверки и переиспользования.

Работы:

- stable `manifest.json`;
- JSONL index всех fetched HTML;
- export command для выбранной коллекции;
- link recovery metadata;
- checksums для HTML.

Готово, когда:

- можно быстро найти все arXiv HTML по collection/library;
- manifest достаточен для повторного attach.

## Phase 6 - Rollout

Порядок:

1. Dry-run на 20 документов Meine.
2. Dry-run на 20 документов Heart and Lung.
3. Dry-run на 20 документов Elvis.
4. Реальный drain по 1 документу из каждой коллекции.
5. Проверка Zotero/WebDAV.
6. Массовый запуск exact-ID only.
7. Включение title search только после audit.


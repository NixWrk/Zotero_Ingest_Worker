# Zotero Metadata Enrichment

Отдельный Python-пакет для обогащения Zotero metadata и поиска доступного полного текста статьи.

Пакет читает локальный `zotero.sqlite` только в read-only режиме, собирает identifiers и evidence из parent item, опрашивает Zotero translators и внешние провайдеры, строит безопасный metadata diff и умеет искать HTML/PDF full text.

## Что умеет

1. Читать parent metadata для PDF attachment или напрямую для parent item.
2. Доставать DOI, PMID, PMCID, ISBN, arXiv ID, URL, title, authors, tags, relations и `extra`.
3. Искать metadata через:
   - Zotero translation-server;
   - Crossref;
   - PubMed;
   - Europe PMC;
   - OpenAlex;
   - Semantic Scholar;
   - Unpaywall;
   - DataCite;
   - bioRxiv/medRxiv;
   - CORE;
   - OpenAIRE;
   - DOAJ;
   - arXiv;
   - OpenCitations для auxiliary citation metadata.
4. Сливать найденные candidates в один `merged` candidate с provenance.
5. Искать HTML статьи, сохранять snapshot с assets и отбрасывать landing/challenge pages.
6. Если HTML не найден, искать PDF, проверять identity по текстовому слою и помечать PDF без текста как `downloaded_needs_ocr`.

## CLI

```powershell
$env:PYTHONPATH='src'
python -m zotero_metadata_enrichment.cli inspect-zotero --data-dir C:\PC\Zotero\Zotero_NIX_Data --attachment-key PDFKEY
python -m zotero_metadata_enrichment.cli inspect-zotero --data-dir C:\PC\Zotero\Zotero_NIX_Data --item-key ITEMKEY
python -m zotero_metadata_enrichment.cli discover-sources --data-dir C:\PC\Zotero\Zotero_NIX_Data --item-key ITEMKEY
python -m zotero_metadata_enrichment.cli download-full-text-sources --data-dir C:\PC\Zotero\Zotero_NIX_Data --item-key ITEMKEY --output-dir D:\tmp\fulltext
```

Все download-команды принимают `--attachment-key` или `--item-key`.

## Конфигурация провайдеров

```env
METADATA_CROSSREF_EMAIL=kalm.nik.v@gmail.com
METADATA_UNPAYWALL_EMAIL=kalm.nik.v@gmail.com
METADATA_OPENALEX_API_KEY=...
METADATA_SEMANTIC_SCHOLAR_API_KEY=
METADATA_CORE_API_KEY=...
ZOTERO_TRANSLATION_SERVER_URL=http://zotero-translation-server:1969
```

Semantic Scholar может работать без API key, но с более строгими лимитами.

## Разработка

```powershell
python -m pytest -q
```

Пакет не должен сам писать в Zotero. Запись metadata/attachments делает основной `zotero-worker` через relay после того, как этот пакет вернул diff или full-text artifact.

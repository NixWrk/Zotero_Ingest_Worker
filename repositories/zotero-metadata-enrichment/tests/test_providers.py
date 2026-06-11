from __future__ import annotations

from zotero_metadata_enrichment.providers.arxiv import parse_arxiv_atom
from zotero_metadata_enrichment.providers.biorxiv import biorxiv_record_to_candidate
from zotero_metadata_enrichment.providers.core import core_work_to_candidate
from zotero_metadata_enrichment.providers.crossref import crossref_work_to_candidate
from zotero_metadata_enrichment.providers.datacite import datacite_record_to_candidate
from zotero_metadata_enrichment.providers.doaj import doaj_bibjson_to_candidate
from zotero_metadata_enrichment.providers.europe_pmc import europe_pmc_result_to_candidate
from zotero_metadata_enrichment.providers.openalex import OpenAlexClient, openalex_work_to_candidate
from zotero_metadata_enrichment.providers.openaire import openaire_payload_to_candidate
from zotero_metadata_enrichment.providers.pubmed import parse_pubmed_xml
from zotero_metadata_enrichment.providers.semantic_scholar import semantic_scholar_paper_to_candidate
from zotero_metadata_enrichment.providers.unpaywall import unpaywall_item_to_candidate
from zotero_metadata_enrichment.providers.zotero_translation_server import (
    zotero_translator_item_to_candidate,
)


def test_parse_arxiv_atom() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.01234v2</id>
        <updated>2024-01-03T00:00:00Z</updated>
        <published>2024-01-01T00:00:00Z</published>
        <title>  A Careful   Metadata Pipeline  </title>
        <summary>  This paper tests metadata. </summary>
        <author><name>Ada Lovelace</name></author>
        <arxiv:primary_category term="cs.DL" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
    </feed>
    """

    candidates = parse_arxiv_atom(xml)

    assert len(candidates) == 1
    assert candidates[0].identifier == "2401.01234"
    assert candidates[0].fields["DOI"] == "10.48550/arXiv.2401.01234"
    assert candidates[0].fields["extra"] == "arXiv:2401.01234 [cs.DL]"


def test_zotero_translator_item_to_candidate() -> None:
    item = {
        "itemType": "journalArticle",
        "title": "A Careful Metadata Pipeline",
        "abstractNote": "<p>This paper tests metadata.</p>",
        "date": "2024",
        "DOI": "https://doi.org/10.1000/example",
        "ISSN": ["1234-5678", "8765-4321"],
        "extra": "arXiv:2401.01234 [cs.DL]",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "publicationTitle": "Journal of Pipelines",
    }

    candidate = zotero_translator_item_to_candidate(
        item,
        source="zotero_translation_server_search",
        identifier="10.1000/example",
        default_score=1.0,
        expected_title="A careful metadata pipeline",
    )

    assert candidate is not None
    assert candidate.fields["DOI"] == "10.1000/example"
    assert candidate.fields["abstractNote"] == "This paper tests metadata."
    assert candidate.fields["ISSN"] == "1234-5678, 8765-4321"
    assert candidate.fields["archiveLocation"] == "2401.01234"
    assert candidate.fields["publicationTitle"] == "Journal of Pipelines"
    assert candidate.raw["publicationTitle"] == "Journal of Pipelines"


def test_zotero_translator_item_includes_attachment_locations() -> None:
    candidate = zotero_translator_item_to_candidate(
        {
            "itemType": "journalArticle",
            "title": "A Careful Metadata Pipeline",
            "DOI": "10.1000/example",
            "url": "https://journal.example/article",
            "attachments": [
                {
                    "title": "Full Text PDF",
                    "mimeType": "application/pdf",
                    "url": "https://journal.example/article.pdf",
                },
                {
                    "title": "Snapshot",
                    "mimeType": "text/html",
                    "url": "https://journal.example/article/full",
                },
            ],
        },
        source="zotero_translation_server_search",
        identifier="10.1000/example",
        default_score=1.0,
        expected_title="A careful metadata pipeline",
    )

    assert candidate is not None
    locations = candidate.raw["full_text_locations"]
    assert locations[0]["kind"] == "landing"
    assert locations[1]["kind"] == "pdf"
    assert locations[2]["kind"] == "html"


def test_parse_pubmed_xml() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>35334517</PMID>
          <Article>
            <Journal>
              <ISSN>0022-5347</ISSN>
              <JournalIssue>
                <Volume>207</Volume>
                <Issue>6</Issue>
                <PubDate><Year>2022</Year><Month>Mar</Month><Day>24</Day></PubDate>
              </JournalIssue>
              <Title>Journal of Urology</Title>
              <ISOAbbreviation>J Urol</ISOAbbreviation>
            </Journal>
            <ArticleTitle>A Careful PubMed Pipeline</ArticleTitle>
            <Pagination><MedlinePgn>100-108</MedlinePgn></Pagination>
            <Abstract>
              <AbstractText Label="Objective">This paper tests PubMed metadata.</AbstractText>
            </Abstract>
            <AuthorList>
              <Author><LastName>Lovelace</LastName><ForeName>Ada</ForeName><Initials>A</Initials></Author>
            </AuthorList>
          </Article>
        </MedlineCitation>
        <PubmedData>
          <ArticleIdList>
            <ArticleId IdType="pubmed">35334517</ArticleId>
            <ArticleId IdType="doi">10.1000/pubmed-example</ArticleId>
            <ArticleId IdType="pmc">PMC9058800</ArticleId>
          </ArticleIdList>
          <PublicationStatus>ppublish</PublicationStatus>
        </PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
    """

    candidates = parse_pubmed_xml(xml)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.identifier == "35334517"
    assert candidate.fields["title"] == "A Careful PubMed Pipeline"
    assert candidate.fields["publicationTitle"] == "Journal of Urology"
    assert candidate.fields["journalAbbreviation"] == "J Urol"
    assert candidate.fields["date"] == "2022-03-24"
    assert candidate.fields["volume"] == "207"
    assert candidate.fields["issue"] == "6"
    assert candidate.fields["pages"] == "100-108"
    assert candidate.fields["DOI"] == "10.1000/pubmed-example"
    assert candidate.fields["PMID"] == "35334517"
    assert candidate.fields["PMCID"] == "PMC9058800"
    assert candidate.fields["abstractNote"] == "Objective: This paper tests PubMed metadata."


def test_unpaywall_candidate_includes_oa_locations() -> None:
    candidate = unpaywall_item_to_candidate(
        {
            "doi": "10.1000/example",
            "title": "OA Example",
            "year": 2024,
            "journal_name": "Journal of Open Things",
            "best_oa_location": {
                "url_for_pdf": "https://repo.example/paper.pdf",
                "url_for_landing_page": "https://repo.example/paper",
                "license": "cc-by",
                "version": "acceptedVersion",
                "host_type": "repository",
            },
            "oa_locations": [],
        }
    )

    assert candidate is not None
    assert candidate.fields["publicationTitle"] == "Journal of Open Things"
    assert candidate.raw["full_text_locations"][0]["kind"] == "pdf"


def test_crossref_candidate_includes_full_text_locations() -> None:
    candidate = crossref_work_to_candidate(
        {
            "DOI": "10.1000/example",
            "title": ["Crossref Example"],
            "URL": "https://doi.org/10.1000/example",
            "link": [
                {
                    "URL": "https://journal.example/article/full",
                    "content-type": "text/html",
                    "content-version": "vor",
                },
                {
                    "URL": "https://journal.example/article.pdf",
                    "content-type": "application/pdf",
                },
            ],
        },
        score=1.0,
    )

    assert candidate is not None
    locations = candidate.raw["full_text_locations"]
    assert locations[0]["kind"] == "html"
    assert locations[0]["content_type"] == "text/html"
    assert locations[1]["kind"] == "pdf"


def test_openalex_candidate_includes_pdf_location_and_abstract() -> None:
    candidate = openalex_work_to_candidate(
        {
            "id": "https://openalex.org/W1",
            "display_name": "OpenAlex Example",
            "doi": "https://doi.org/10.1000/example",
            "publication_date": "2024-03-02",
            "abstract_inverted_index": {"This": [0], "works": [1]},
            "biblio": {"volume": "12", "issue": "3", "first_page": "10", "last_page": "20"},
            "primary_location": {
                "landing_page_url": "https://publisher.example/article",
                "pdf_url": "https://repo.example/article.pdf",
                "is_oa": True,
                "license": "cc-by",
                "source": {"display_name": "Repository", "issn": ["1234-5678"]},
            },
        },
        identifier="10.1000/example",
        score=1.0,
    )

    assert candidate is not None
    assert candidate.fields["abstractNote"] == "This works"
    assert candidate.fields["pages"] == "10-20"
    assert candidate.raw["full_text_locations"][0]["kind"] == "pdf"


def test_openalex_client_adds_mailto_and_api_key_params() -> None:
    client = OpenAlexClient(mailto="owner@example.com", api_key="openalex-key")

    assert client._polite_params() == {
        "mailto": "owner@example.com",
        "api_key": "openalex-key",
    }


def test_europe_pmc_candidate_adds_pmc_xml_location() -> None:
    candidate = europe_pmc_result_to_candidate(
        {
            "title": "Europe PMC Example",
            "doi": "10.1000/example",
            "pmid": "35334517",
            "pmcid": "PMC9058800",
            "journalTitle": "BMJ Open",
            "isOpenAccess": "Y",
            "hasFullText": "Y",
        }
    )

    assert candidate is not None
    assert candidate.fields["PMCID"] == "PMC9058800"
    assert any(location["source"] == "pmc_oai" and location["kind"] == "xml" for location in candidate.raw["full_text_locations"])


def test_semantic_scholar_candidate_includes_open_access_pdf() -> None:
    candidate = semantic_scholar_paper_to_candidate(
        {
            "paperId": "abc",
            "title": "Semantic Scholar Example",
            "abstract": "A short abstract.",
            "externalIds": {"DOI": "10.1000/example", "PubMed": "35334517"},
            "journal": {"name": "Journal", "volume": "1", "pages": "2-3"},
            "openAccessPdf": {"url": "https://example.org/paper.pdf"},
        },
        identifier="DOI:10.1000/example",
        score=1.0,
    )

    assert candidate is not None
    assert candidate.fields["PMID"] == "35334517"
    assert candidate.raw["full_text_locations"][0]["kind"] == "pdf"


def test_datacite_candidate_uses_content_urls() -> None:
    candidate = datacite_record_to_candidate(
        {
            "id": "10.1000/example",
            "attributes": {
                "doi": "10.1000/example",
                "titles": [{"title": "DataCite Example"}],
                "publicationYear": 2024,
                "publisher": "Repository",
                "url": "https://repo.example/landing",
                "contentUrl": ["https://repo.example/file.pdf"],
            },
        }
    )

    assert candidate is not None
    assert candidate.fields["publisher"] == "Repository"
    assert candidate.raw["full_text_locations"][0]["kind"] == "pdf"


def test_preprint_and_repository_provider_parsers() -> None:
    biorxiv = biorxiv_record_to_candidate(
        {"doi": "10.1101/2025.01.01.123456", "title": "Preprint", "abstract": "Preprint abstract.", "date": "2025-01-01"},
        server="biorxiv",
    )
    core = core_work_to_candidate(
        {"title": "CORE Paper", "doi": "10.1000/core", "downloadUrl": "https://core.example/paper.pdf"},
        identifier="10.1000/core",
        score=1.0,
    )

    assert biorxiv is not None
    assert biorxiv.fields["repository"] == "bioRxiv"
    assert any(location["kind"] == "pdf" for location in biorxiv.raw["full_text_locations"])
    assert core is not None
    assert core.raw["full_text_locations"][0]["source"] == "core"


def test_doaj_and_openaire_parsers() -> None:
    doaj = doaj_bibjson_to_candidate(
        {
            "title": "DOAJ Example",
            "identifier": [{"type": "doi", "id": "10.1000/doaj"}],
            "journal": {"title": "Open Journal", "issns": ["1234-5678"], "publisher": "Open Publisher"},
            "link": [{"url": "https://journal.example/article.pdf", "type": "fulltext"}],
        },
        identifier="10.1000/doaj",
        score=1.0,
    )
    openaire = openaire_payload_to_candidate(
        {
            "response": {
                "results": {
                    "result": {
                        "metadata": {
                            "oaf:entity": {
                                "oaf:result": {
                                    "title": {"$": "OpenAIRE Example"},
                                    "pid": [{"@classid": "doi", "$": "10.1000/openaire"}],
                                    "children": {"instance": {"url": {"$": "https://repo.example/item"}}},
                                }
                            }
                        }
                    }
                }
            }
        },
        identifier="10.1000/openaire",
    )

    assert doaj is not None
    assert doaj.fields["publicationTitle"] == "Open Journal"
    assert openaire is not None
    assert openaire.raw["full_text_locations"][0]["url"] == "https://repo.example/item"

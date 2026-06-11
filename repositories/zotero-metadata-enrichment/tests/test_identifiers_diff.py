from __future__ import annotations

from zotero_metadata_enrichment import MetadataCandidate, build_metadata_diff, build_metadata_patch
from zotero_metadata_enrichment.identifiers import (
    extract_arxiv_id_from_text,
    extract_doi_from_text,
    extract_isbn_from_text,
    extract_pmcid_from_text,
    extract_pmid_from_text,
)
from zotero_metadata_enrichment.text import title_match_score


def test_extract_identifiers() -> None:
    text = """
    DOI: https://doi.org/10.48550/arXiv.2401.01234v2
    Also available at https://arxiv.org/pdf/cs/9901001.pdf
    PMID: 35334517
    PMCID: PMC9058800
    ISBN: 978-3-642-31534-3
    """

    assert extract_doi_from_text(text) == "10.48550/arXiv.2401.01234v2"
    assert extract_arxiv_id_from_text(text) == "2401.01234"
    assert extract_pmid_from_text(text) == "35334517"
    assert extract_pmcid_from_text(text) == "PMC9058800"
    assert extract_isbn_from_text(text) == "9783642315343"


def test_metadata_diff_and_patch() -> None:
    candidate = MetadataCandidate(
        source="crossref",
        identifier="10.1000/example",
        score=1.0,
        fields={
            "title": "New title",
            "DOI": "10.1000/example",
            "publicationTitle": "Journal of Examples",
            "extra": "arXiv:2401.01234",
        },
    )

    diff = build_metadata_diff(
        candidate,
        current_fields={"title": "Existing title", "extra": "Original line"},
        policy="emptyFieldsOnly",
    )

    assert diff["patch"] == {"DOI": "10.1000/example", "publicationTitle": "Journal of Examples"}
    assert diff["skipped_fields"]["title"] == "current_field_not_empty"
    assert build_metadata_patch(candidate, current_fields={}, policy="allowOverwrite")["title"] == "New title"


def test_metadata_diff_does_not_duplicate_structured_identifier_in_extra() -> None:
    candidate = MetadataCandidate(
        source="pubmed",
        identifier="17193894",
        score=1.0,
        fields={"PMID": "17193894", "extra": "PMID: 17193894"},
    )

    diff = build_metadata_diff(
        candidate,
        current_fields={"PMID": "17193894"},
        policy="emptyFieldsOnly",
    )

    assert diff["patch"] == {}
    assert diff["skipped_fields"]["PMID"] == "current_field_not_empty"
    assert diff["skipped_fields"]["extra"] == "candidate_empty"


def test_title_score() -> None:
    assert title_match_score("A careful metadata pipeline", "A Careful Metadata Pipeline") == 1.0
    assert title_match_score("Completely different", "A Careful Metadata Pipeline") < 0.5

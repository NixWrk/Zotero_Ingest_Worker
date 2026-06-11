from __future__ import annotations

from zotero_arxiv_html_ingest.identifiers import extract_arxiv_id_from_text, normalize_arxiv_id
from zotero_arxiv_html_ingest.lookup import parse_arxiv_atom


def test_extract_arxiv_id_prefers_doi_identifier() -> None:
    text = """
    DOI: https://doi.org/10.48550/arXiv.2401.01234v2
    older mirror: https://arxiv.org/pdf/cs/9901001.pdf
    """

    assert extract_arxiv_id_from_text(text) == "2401.01234"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2401.01234v3.pdf") == "2401.01234"


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
    assert candidates[0].arxiv_id == "2401.01234"
    assert candidates[0].title == "A Careful Metadata Pipeline"
    assert candidates[0].doi == "10.48550/arXiv.2401.01234"
    assert candidates[0].raw["primary_category"] == "cs.DL"


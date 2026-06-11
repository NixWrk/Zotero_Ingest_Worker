from .arxiv import ArxivClient, parse_arxiv_atom
from .biorxiv import BioRxivClient, biorxiv_record_to_candidate
from .core import CoreClient, core_work_to_candidate
from .crossref import CrossrefClient
from .datacite import DataCiteClient, datacite_record_to_candidate
from .doaj import DoajClient, doaj_bibjson_to_candidate
from .europe_pmc import EuropePmcClient, europe_pmc_result_to_candidate
from .openalex import OpenAlexClient, openalex_work_to_candidate
from .openaire import OpenAireClient, openaire_payload_to_candidate
from .opencitations import OpenCitationsClient
from .pubmed import PubMedClient, parse_pubmed_xml
from .semantic_scholar import SemanticScholarClient, semantic_scholar_paper_to_candidate
from .unpaywall import UnpaywallClient, unpaywall_item_to_candidate
from .zotero_translation_server import TranslationServerClient, zotero_translator_item_to_candidate

__all__ = [
    "ArxivClient",
    "BioRxivClient",
    "CoreClient",
    "CrossrefClient",
    "DataCiteClient",
    "DoajClient",
    "EuropePmcClient",
    "OpenAireClient",
    "OpenAlexClient",
    "OpenCitationsClient",
    "PubMedClient",
    "SemanticScholarClient",
    "TranslationServerClient",
    "UnpaywallClient",
    "biorxiv_record_to_candidate",
    "core_work_to_candidate",
    "datacite_record_to_candidate",
    "doaj_bibjson_to_candidate",
    "europe_pmc_result_to_candidate",
    "openalex_work_to_candidate",
    "openaire_payload_to_candidate",
    "parse_arxiv_atom",
    "parse_pubmed_xml",
    "semantic_scholar_paper_to_candidate",
    "unpaywall_item_to_candidate",
    "zotero_translator_item_to_candidate",
]

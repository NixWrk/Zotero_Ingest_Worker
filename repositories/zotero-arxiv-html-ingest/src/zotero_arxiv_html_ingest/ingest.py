from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .html_fetch import ArxivHtmlClient
from .identifiers import extract_arxiv_id_from_text
from .lookup import ArxivLookupClient
from .models import ArxivCandidate, ArxivHtmlArtifact, LocalAttachment
from .relay import ZoteroRelayClient
from .storage import arxiv_html_filename, copy_relay_sibling_local, write_arxiv_html_artifact


@dataclass(frozen=True)
class IngestConfig:
    html_root: Path
    arxiv_search_min_score: float = 0.88
    attach: bool = True


class ArxivHtmlIngestor:
    def __init__(
        self,
        config: IngestConfig,
        *,
        lookup: ArxivLookupClient | None = None,
        html: ArxivHtmlClient | None = None,
        relay: ZoteroRelayClient | None = None,
    ) -> None:
        self.config = config
        self.lookup = lookup or ArxivLookupClient()
        self.html = html or ArxivHtmlClient()
        self.relay = relay

    def lookup_candidate(
        self,
        *,
        metadata_text: str,
        title: str = "",
    ) -> ArxivCandidate | None:
        arxiv_id = extract_arxiv_id_from_text(metadata_text)
        if arxiv_id:
            return self.lookup.by_id(arxiv_id) or ArxivCandidate(
                arxiv_id=arxiv_id,
                score=1.0,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                doi=f"10.48550/arXiv.{arxiv_id}",
                source="metadata",
                raw={"match": "identifier"},
            )
        if not title:
            return None
        candidate = self.lookup.by_title(title)
        if candidate is None or candidate.score < self.config.arxiv_search_min_score:
            return None
        return candidate

    def ingest(
        self,
        *,
        attachment: LocalAttachment,
        candidate: ArxivCandidate,
    ) -> dict[str, object]:
        html_text, validation = self.html.fetch(candidate.arxiv_id)
        artifact = write_arxiv_html_artifact(
            root=self.config.html_root,
            attachment=attachment,
            candidate=candidate,
            html_text=html_text,
            validation=validation,
        )
        relay_result: dict[str, object] | None = None
        local_copy: dict[str, object] | None = None
        if self.config.attach:
            if self.relay is None:
                raise RuntimeError("Relay client is required when attach=True.")
            filename = arxiv_html_filename(attachment.filename)
            relay_result = self.relay.add_html_sibling(
                attachment=attachment,
                source_path=artifact.path,
                arxiv_id=candidate.arxiv_id,
                filename=filename,
                title=filename,
            )
            local_copy = copy_relay_sibling_local(
                attachment=attachment,
                source_path=artifact.path,
                filename=filename,
                relay_result=relay_result,
            )
        return {
            "ok": True,
            "artifact": artifact.to_dict(),
            "relay": relay_result,
            "local_copy": local_copy,
        }


def artifact_from_existing(
    *,
    root: Path,
    attachment: LocalAttachment,
    candidate: ArxivCandidate,
    html_text: str,
    validation: dict[str, object],
) -> ArxivHtmlArtifact:
    return write_arxiv_html_artifact(
        root=root,
        attachment=attachment,
        candidate=candidate,
        html_text=html_text,
        validation=validation,
    )


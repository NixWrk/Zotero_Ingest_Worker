"""Publisher handler registry for web-native HTML polishing."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType

from .core import WebArticleExtraction, WebHtmlKind


@dataclass(frozen=True)
class WebPolishHandler:
    """Lazy adapter for one web-native HTML source family."""

    kind: WebHtmlKind
    module_name: str | None = None
    default_origin: str | None = None
    rejection_message: str | None = None

    @property
    def rejects_full_text(self) -> bool:
        return self.rejection_message is not None

    def module(self) -> ModuleType:
        if self.module_name is None:
            raise LookupError(f"{self.kind.value} has no publisher polish module")
        return import_module(f"{__package__}.{self.module_name}")

    def extract_article_fragment(self, html: str) -> WebArticleExtraction | None:
        if self.module_name is None:
            return None
        return self.module().extract_article_fragment(html)

    def normalize_article_fragment(
        self,
        html: str,
        *,
        source_url: str | None = None,
        canonical_url: str | None = None,
    ) -> str:
        if self.module_name is None:
            return html
        return self.module().normalize_article_fragment(
            html,
            source_url=source_url,
            canonical_url=canonical_url,
        )


_HANDLERS: dict[WebHtmlKind, WebPolishHandler] = {
    WebHtmlKind.ARXIV_ABS_PAGE: WebPolishHandler(
        WebHtmlKind.ARXIV_ABS_PAGE,
        rejection_message="arXiv abstract pages are landing pages, not article HTML; use the /html/ attachment instead.",
    ),
    WebHtmlKind.ARXIV_LATEXML: WebPolishHandler(
        WebHtmlKind.ARXIV_LATEXML,
        module_name="arxiv",
        default_origin="https://arxiv.org/",
    ),
    WebHtmlKind.PMC_ARTICLE: WebPolishHandler(
        WebHtmlKind.PMC_ARTICLE,
        module_name="pmc",
        default_origin="https://pmc.ncbi.nlm.nih.gov/",
    ),
    WebHtmlKind.TAYLOR_FRANCIS_ARTICLE: WebPolishHandler(
        WebHtmlKind.TAYLOR_FRANCIS_ARTICLE,
        module_name="taylor_francis",
        default_origin="https://www.tandfonline.com/",
    ),
    WebHtmlKind.SPRINGER_NATURE_ARTICLE: WebPolishHandler(
        WebHtmlKind.SPRINGER_NATURE_ARTICLE,
        module_name="springer_nature",
        default_origin="https://link.springer.com/",
    ),
    WebHtmlKind.IOP_ARTICLE: WebPolishHandler(
        WebHtmlKind.IOP_ARTICLE,
        module_name="iop",
        default_origin="https://iopscience.iop.org/",
    ),
    WebHtmlKind.RESEARCHGATE_PAGE: WebPolishHandler(
        WebHtmlKind.RESEARCHGATE_PAGE,
        rejection_message="ResearchGate pages are landing/PDF pages, not stable article HTML; use the PDF attachment when available.",
    ),
    WebHtmlKind.SCIENDO_ABSTRACT_PAGE: WebPolishHandler(
        WebHtmlKind.SCIENDO_ABSTRACT_PAGE,
        rejection_message="Sciendo/Reference Global abstract-tab pages are not full article HTML; fetch the ?tab=article URL first.",
    ),
    WebHtmlKind.OJS_ABSTRACT_PAGE: WebPolishHandler(
        WebHtmlKind.OJS_ABSTRACT_PAGE,
        rejection_message="OJS article pages with only abstract/galley links are not full article HTML; use the PDF galley when available.",
    ),
}


def registered_web_polish_handlers() -> tuple[WebPolishHandler, ...]:
    """Return all registered source handlers in deterministic order."""

    return tuple(_HANDLERS.values())


def handler_for_kind(kind: WebHtmlKind | None) -> WebPolishHandler | None:
    if kind is None:
        return None
    return _HANDLERS.get(kind)


def default_origin_for_kind(kind: WebHtmlKind) -> str | None:
    handler = handler_for_kind(kind)
    if handler is None:
        return None
    return handler.default_origin


def rejection_message_for_kind(kind: WebHtmlKind) -> str | None:
    handler = handler_for_kind(kind)
    if handler is None:
        return None
    return handler.rejection_message


def extract_source_specific_article_fragment(
    html: str,
    *,
    kind: WebHtmlKind | None,
) -> WebArticleExtraction | None:
    handler = handler_for_kind(kind)
    if handler is None:
        return None
    return handler.extract_article_fragment(html)


def normalize_source_specific_article_fragment(
    html: str,
    *,
    kind: WebHtmlKind,
    source_url: str | None = None,
    canonical_url: str | None = None,
) -> str:
    handler = handler_for_kind(kind)
    if handler is None:
        return html
    return handler.normalize_article_fragment(
        html,
        source_url=source_url,
        canonical_url=canonical_url,
    )

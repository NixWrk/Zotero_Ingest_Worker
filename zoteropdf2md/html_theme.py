"""Shared HTML presentation snippets for polished article documents."""

from __future__ import annotations


WEB_READABILITY_STYLE = """<style data-z2m-style="web-html-polish">
:root { color-scheme: light; }
body {
  margin: 0;
  background: #f7f8fa;
  color: #171717;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.6;
}
#web-doc {
  box-sizing: border-box;
  max-width: 980px;
  margin: 0 auto;
  padding: 32px 24px 56px;
  background: #fff;
}
img, svg, video, canvas { max-width: 100%; height: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #d8dde6; padding: 6px 8px; vertical-align: top; }
pre, code { white-space: pre-wrap; overflow-wrap: anywhere; }
a { color: #0645ad; overflow-wrap: anywhere; }
figure,
.fig,
.fig-inline,
.tbl,
.table-wrap,
.tableView,
.NLM_table-wrap,
.NLM_table,
.c-article-section__figure,
.c-article-section__table,
.c-article-table,
figure.ltx_table {
  margin: 28px 0;
  padding: 14px 0;
  border-top: 1px solid #cbd5e1;
  border-bottom: 1px solid #cbd5e1;
  background: #fff;
  clear: both;
}
.c-article-section__figure figure,
.c-article-section__table figure,
.c-article-table figure,
figure figure {
  margin: 0;
  padding: 0;
  border: 0;
}
figcaption,
caption,
.fig-caption,
.table-caption,
.caption,
.captionText,
.tableCaption,
.NLM_caption,
.c-article-table__caption,
.ltx_caption {
  display: block;
  margin: 0 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid #e5e7eb;
  color: #374151;
  font-size: 0.95em;
  line-height: 1.5;
}
.captionLabel,
.fig-label,
.table-label,
.ltx_tag_caption,
.c-article-section__figure-caption,
.c-article-section__table-caption,
.c-article-table__caption b {
  color: #111827;
  font-weight: 650;
}
.c-article-references,
ul.ref-list,
ul.references.numeric-ordered-list,
.ref-list > ul,
.references > ul {
  list-style: none;
  counter-reset: z2m-ref;
  padding-left: 0;
}
.c-article-references > li,
ul.ref-list > li,
ul.references.numeric-ordered-list > li,
.ref-list > ul > li,
.references > ul > li {
  counter-increment: z2m-ref;
  position: relative;
  padding-left: 2.8em;
  margin: 0.6em 0;
}
.c-article-references > li::before,
ul.ref-list > li::before,
ul.references.numeric-ordered-list > li::before,
.ref-list > ul > li::before,
.references > ul > li::before {
  content: counter(z2m-ref) ".";
  position: absolute;
  left: 0;
  width: 2.2em;
  text-align: right;
  color: #4b5563;
  font-weight: 600;
}
.off-screen, .sr-only, .visually-hidden, .u-visually-hidden, .usa-sr-only {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
figure.ltx_table { overflow-x: auto; }
figure.ltx_table .ltx_transformed_outer {
  width: 100% !important;
  max-width: 100% !important;
  height: auto !important;
  vertical-align: baseline !important;
  overflow-x: auto;
}
figure.ltx_table .ltx_transformed_inner {
  display: block;
  transform: none !important;
}
figure.ltx_table table {
  width: auto;
  max-width: 100%;
  margin: 0 auto;
}
.ltx_align_center { text-align: center; }
.ltx_align_left { text-align: left; }
.ltx_align_right { text-align: right; }
#web-doc :target {
  outline: 3px solid #f59e0b;
  outline-offset: 4px;
  background: #fff7d6;
  border-radius: 4px;
  scroll-margin-top: 24px;
}
</style>"""


def web_readability_style() -> str:
    """Return the stable CSS wrapper used for web-native polished HTML."""

    return WEB_READABILITY_STYLE

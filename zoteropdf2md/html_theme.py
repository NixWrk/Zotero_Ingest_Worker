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
.ArticleReference.z2m-frontiers-citation {
  display: inline;
  border: 0;
  padding: 0;
  background: transparent;
  color: #0645ad;
  font: inherit;
  text-decoration: underline;
  text-underline-offset: 0.12em;
  cursor: pointer;
}
dl.def-list {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  column-gap: 1.5em;
  row-gap: 0.4em;
  align-items: baseline;
}
dl.def-list dt {
  grid-column: 1;
  font-weight: 650;
}
dl.def-list dd {
  grid-column: 2;
  margin: 0;
}
dl.def-list dd p { margin: 0; }
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
.References {
  list-style: none;
  padding-left: 0;
  margin: 1em 0;
}
.References__item {
  display: grid;
  grid-template-columns: 2.6em minmax(0, 1fr);
  column-gap: 0.85em;
  align-items: start;
  margin: 0.75em 0;
}
.References__label p {
  margin: 0;
  color: #4b5563;
  font-weight: 650;
  text-align: right;
}
.References__content p {
  margin: 0 0 0.3em;
}
.References__personGroup,
.References__name,
.References__surname,
.References__givenNames {
  display: inline;
}
.References__links {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25em 0.8em;
  list-style: none;
  padding-left: 0;
  margin: 0.2em 0 0;
  font-size: 0.92em;
}
.References__links__item {
  margin: 0;
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
figure.ltx_table {
  box-sizing: border-box;
  width: 100% !important;
  max-width: 100% !important;
  overflow-x: auto;
}
figure.ltx_table > figcaption,
figure.ltx_table > .ltx_caption {
  box-sizing: border-box;
  width: 100%;
}
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
.ltx_caption .ltx_inline-block,
.ltx_caption .ltx_transformed_outer,
figcaption .ltx_inline-block,
figcaption .ltx_transformed_outer {
  display: inline !important;
  width: auto !important;
  max-width: none !important;
  height: auto !important;
  vertical-align: baseline !important;
}
.ltx_caption .ltx_transformed_inner,
figcaption .ltx_transformed_inner {
  display: inline !important;
  transform: none !important;
}
.ltx_caption .ltx_p,
figcaption .ltx_p {
  display: inline;
  margin: 0;
}
figure.ltx_table table {
  width: max-content;
  min-width: 100%;
  max-width: none;
  margin: 0 auto;
}
.ltx_itemize,
.ltx_enumerate {
  padding-left: 0;
  list-style: none;
}
.ltx_item {
  display: grid;
  grid-template-columns: 1.35em minmax(0, 1fr);
  column-gap: 0.45em;
  align-items: baseline;
  margin: 0.45em 0;
}
.ltx_item > .ltx_tag_item {
  grid-column: 1;
  color: #4b5563;
  font-weight: 650;
  text-align: center;
}
.ltx_item > .ltx_para {
  grid-column: 2;
}
.ltx_item > .ltx_para,
.ltx_item > .ltx_para > .ltx_p {
  margin: 0;
}
table.ltx_equation,
table.ltx_equationgroup {
  width: 100%;
  max-width: 100%;
  margin: 1em 0;
  border: 0;
  border-collapse: collapse;
  background: transparent;
}
table.ltx_equation td,
table.ltx_equation th,
table.ltx_equationgroup td,
table.ltx_equationgroup th {
  border: 0;
  padding: 0.15em 0.35em;
  background: transparent;
  vertical-align: middle;
}
table.ltx_equation .ltx_eqn_center_padleft,
table.ltx_equation .ltx_eqn_center_padright,
table.ltx_equationgroup .ltx_eqn_center_padleft,
table.ltx_equationgroup .ltx_eqn_center_padright {
  width: 1%;
  padding: 0;
}
table.ltx_equation .ltx_eqn_eqno,
table.ltx_equationgroup .ltx_eqn_eqno {
  width: 1%;
  padding-left: 0.75em;
  white-space: nowrap;
  text-align: right;
}
.ltx_align_center { text-align: center; }
.ltx_align_left { text-align: left; }
.ltx_align_right { text-align: right; }
table.ltx_equation .z2m-ltx-single-equation-cell,
table.ltx_equationgroup .z2m-ltx-single-equation-cell {
  text-align: center;
}
table.disp-formula {
  width: 100%;
  max-width: 100%;
  margin: 1em 0;
  border: 0;
  border-collapse: collapse;
  background: transparent;
}
table.disp-formula td,
table.disp-formula th {
  border: 0;
  padding: 0.15em 0.35em;
  background: transparent;
  vertical-align: middle;
}
table.disp-formula td.formula {
  text-align: center;
  overflow-x: auto;
}
table.disp-formula td.label {
  width: 1%;
  padding-left: 0.75em;
  white-space: nowrap;
  text-align: right;
}
math[display="block"] {
  display: block;
  max-width: 100%;
  overflow-x: auto;
}
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

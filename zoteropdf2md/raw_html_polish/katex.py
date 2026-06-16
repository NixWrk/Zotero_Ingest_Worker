"""Static KaTeX rendering for polished HTML output."""

from __future__ import annotations

import atexit
from collections.abc import Callable
import functools
import html as html_lib
from pathlib import Path
import re
from typing import Any

from ..html_links import escape_html_attr_literal
from .html_fragments import MATH_TAG_SPLIT_PATTERN, update_skip_stack_for_tags


HEAD_CLOSE_PATTERN = re.compile(r"</head>", re.IGNORECASE)
SKIP_MATH_RENDER_TAGS = {"script", "style", "code", "pre", "math", "svg", "a"}
MATHJAX_SCRIPT = (
    '<script>'
    'MathJax={'
    'tex:{inlineMath:[["$","$"],["\\\\(","\\\\)"]],displayMath:[["$$","$$"],["\\\\[","\\\\]"]]},'
    'svg:{fontCache:"global"}'
    '};'
    '</script>\n'
    '<script id="MathJax-script" async '
    'src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>'
)

KATEX_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "katex"
KATEX_STYLE_MARKER = 'data-z2m-style="katex"'
STATIC_DISPLAY_TEX_PATTERN = re.compile(r"\\\[(?P<body>[\s\S]*?)\\\]")
STATIC_INLINE_TEX_PATTERN = re.compile(r"\\\((?P<body>[\s\S]*?)\\\)")
KATEX_PLACEHOLDER_PATTERN = re.compile(r"\ue000Z2MK([0-9]+)\ue001")
MATHJAX_SCRIPT_TAG_PATTERN = re.compile(
    r'<script\b[^>]*\bid\s*=\s*["\']MathJax-script["\'][^>]*>[\s\S]*?</script>|'
    r'<script\b[^>]*\bid\s*=\s*["\']MathJax-script["\'][^>]*/?>',
    re.IGNORECASE,
)
MATHJAX_CONFIG_TAG_PATTERN = re.compile(
    r"<script\b[^>]*>[\s\S]*?\bMathJax\s*=[\s\S]*?</script>",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1)
def katex_inlined_css() -> str:
    return (KATEX_ASSET_DIR / "katex.inlined.css").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def katex_v8_context() -> Any:
    """Embedded-V8 KaTeX context. Built once per process."""

    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval((KATEX_ASSET_DIR / "katex.min.js").read_text(encoding="utf-8"))
    ctx.eval(
        "globalThis.__z2m_katex=function(items){return items.map(function(it){"
        "try{return katex.renderToString(it.t,{displayMode:!!it.d,"
        "throwOnError:false,output:'html'});}"
        "catch(e){return '<span class=\"z2m-math-error\">'"
        "+String(e&&e.message||e)+'</span>';}});};"
    )
    return ctx


def close_katex_v8_context() -> None:
    """Close the cached MiniRacer context so CLI processes can exit cleanly."""

    if katex_v8_context.cache_info().currsize == 0:
        return

    try:
        ctx = katex_v8_context()
    except Exception:
        katex_v8_context.cache_clear()
        return

    try:
        close = getattr(ctx, "close", None)
        if callable(close):
            close()
    finally:
        katex_v8_context.cache_clear()


atexit.register(close_katex_v8_context)


def strip_mathjax_scripts(html: str) -> str:
    html = MATHJAX_SCRIPT_TAG_PATTERN.sub("", html)
    return MATHJAX_CONFIG_TAG_PATTERN.sub("", html)


def inject_mathjax(html: str, *, ensure_head: Callable[[str], str]) -> str:
    if 'MathJax-script' in html:
        html = re.sub(
            r'<script[^>]*id="MathJax-script"[^>]*/?>.*?(?:</script>)?',
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        html = re.sub(
            r'<script[^>]*>[^<]*MathJax\s*=[^<]*</script>',
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not HEAD_CLOSE_PATTERN.search(html):
        html = ensure_head(html)
    return HEAD_CLOSE_PATTERN.sub(lambda _: f"{MATHJAX_SCRIPT}\n</head>", html, count=1)


def inject_katex_css(html: str, *, ensure_head: Callable[[str], str]) -> str:
    if KATEX_STYLE_MARKER in html:
        return html
    style = f"<style {KATEX_STYLE_MARKER}>\n{katex_inlined_css()}\n</style>"
    if not HEAD_CLOSE_PATTERN.search(html):
        html = ensure_head(html)
    return HEAD_CLOSE_PATTERN.sub(lambda _: f"{style}\n</head>", html, count=1)


def render_katex_html(html: str, *, ensure_head: Callable[[str], str]) -> str:
    r"""Replace ``\(...\)`` / ``\[...\]`` TeX with static KaTeX HTML."""

    original = html
    html = strip_mathjax_scripts(html)
    if "\\(" not in html and "\\[" not in html:
        return html

    jobs: list[tuple[str, bool]] = []

    def mask_segment(text: str) -> str:
        def collect(match: re.Match[str], display: bool) -> str:
            jobs.append((html_lib.unescape(match.group("body")), display))
            return f"\ue000Z2MK{len(jobs) - 1}\ue001"

        text = STATIC_DISPLAY_TEX_PATTERN.sub(lambda m: collect(m, True), text)
        return STATIC_INLINE_TEX_PATTERN.sub(lambda m: collect(m, False), text)

    parts = MATH_TAG_SPLIT_PATTERN.split(html)
    skip_stack: list[str] = []
    for idx, part in enumerate(parts):
        if not part:
            continue
        if MATH_TAG_SPLIT_PATTERN.fullmatch(part):
            update_skip_stack_for_tags(part, skip_stack, SKIP_MATH_RENDER_TAGS)
            continue
        if skip_stack:
            continue
        if "\\(" in part or "\\[" in part:
            parts[idx] = mask_segment(part)

    if not jobs:
        return original

    try:
        ctx = katex_v8_context()
    except ImportError:
        return inject_mathjax(original, ensure_head=ensure_head)

    rendered = ctx.call(
        "__z2m_katex", [{"t": tex, "d": display} for tex, display in jobs]
    )

    def expand(match: re.Match[str]) -> str:
        job_index = int(match.group(1))
        tex, display = jobs[job_index]
        body = rendered[job_index] if job_index < len(rendered) else ""
        css_class = (
            "z2m-math z2m-math-display" if display else "z2m-math z2m-math-inline"
        )
        delimited = f"\\[{tex}\\]" if display else f"\\({tex}\\)"
        attr = escape_html_attr_literal(delimited)
        return (
            f'<span class="{css_class}" role="math" '
            f'data-z2m-tex="{attr}">{body}</span>'
        )

    out = KATEX_PLACEHOLDER_PATTERN.sub(expand, "".join(parts))
    return inject_katex_css(out, ensure_head=ensure_head)

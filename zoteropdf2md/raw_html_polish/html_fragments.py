"""Small HTML-fragment utilities shared by raw HTML polish passes."""

from __future__ import annotations

from collections.abc import Callable
import re


TAG_SPLIT_PATTERN = re.compile(r"(<[^>]+>)")
OPEN_TAG_PATTERN = re.compile(r"^<\s*([a-zA-Z0-9:_-]+)")
CLOSE_TAG_PATTERN = re.compile(r"^<\s*/\s*([a-zA-Z0-9:_-]+)")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# Splits on real HTML tags only. Unlike TAG_SPLIT_PATTERN, this never mistakes a
# bare "<" from math text ("a < b", "x < 0") for a tag.
MATH_TAG_SPLIT_PATTERN = re.compile(
    r"(<!--[\s\S]*?-->|<![^<>]*>|</?[A-Za-z][^<>]*>)"
)
FLOAT_NODE_PATTERN = re.compile(
    r'^(?P<open><(?P<tag>p|h[1-6]|table)\b[^>]*>)(?P<body>[\s\S]*)(?P<close></(?P=tag)>)$',
    re.IGNORECASE,
)
DIV_TAG_PATTERN = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)


def visible_text(fragment: str) -> str:
    text = HTML_TAG_PATTERN.sub(" ", fragment)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&#160;", " ")
        .replace("\u00a0", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


def update_skip_stack_for_tags(
    tag_fragment: str,
    skip_stack: list[str],
    skip_tags: set[str],
) -> None:
    raw = tag_fragment[:256].lstrip()
    if not raw.startswith("<") or raw.startswith("<!--") or raw.startswith("<!"):
        return

    close_match = CLOSE_TAG_PATTERN.match(raw)
    if close_match is not None:
        tag_name = close_match.group(1).lower()
        for idx in range(len(skip_stack) - 1, -1, -1):
            if skip_stack[idx] == tag_name:
                del skip_stack[idx]
                break
        return

    if raw.endswith("/>"):
        return

    open_match = OPEN_TAG_PATTERN.match(raw)
    if open_match is None:
        return
    tag_name = open_match.group(1).lower()
    if tag_name in skip_tags:
        skip_stack.append(tag_name)


def append_class_to_attrs(attrs: str, class_name: str) -> str:
    class_match = re.search(r'(\bclass\s*=\s*["\'])([^"\']*)(["\'])', attrs, re.IGNORECASE)
    if class_match is None:
        return f'{attrs} class="{class_name}"'
    classes = class_match.group(2).split()
    if class_name in classes:
        return attrs
    merged = " ".join(classes + [class_name]).strip()
    return attrs[: class_match.start(2)] + merged + attrs[class_match.end(2) :]


def add_body_class(html: str, class_name: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"<body{append_class_to_attrs(match.group(1), class_name)}>"

    return re.sub(r"<body(?P<attrs>[^>]*)>", lambda m: replace(m), html, count=1, flags=re.IGNORECASE)


def has_id_attr(open_tag: str) -> bool:
    return bool(re.search(r"\bid\s*=", open_tag, re.IGNORECASE))


def add_id_attr(open_tag: str, value: str) -> str:
    if has_id_attr(open_tag):
        return open_tag
    if open_tag.endswith(">"):
        return f'{open_tag[:-1]} id="{value}">'
    return f'{open_tag} id="{value}">'


def add_class_attr(open_tag: str, class_name: str) -> str:
    match = re.match(r"^(<[\w:-]+)(?P<attrs>[\s\S]*?)(>)$", open_tag)
    if match is None:
        return open_tag
    return f"{match.group(1)}{append_class_to_attrs(match.group('attrs'), class_name)}>"


def remove_id_attr(open_tag: str) -> str:
    return re.sub(r'\s+\bid\s*=\s*(["\'])[^"\']*\1', "", open_tag, count=1, flags=re.IGNORECASE)


def node_id_value(raw: str) -> str | None:
    match = re.search(r'\bid\s*=\s*(["\'])([^"\']+)\1', raw, re.IGNORECASE)
    return match.group(2) if match is not None else None


def node_open_id_value(raw: str) -> str | None:
    match = FLOAT_NODE_PATTERN.match(raw)
    if match is None:
        return node_id_value(raw)
    id_match = re.search(r'\bid\s*=\s*(["\'])([^"\']+)\1', match.group("open"), re.IGNORECASE)
    return id_match.group(2) if id_match is not None else None


def node_has_class(raw: str, class_name: str) -> bool:
    open_end = raw.find(">")
    attrs = raw[: open_end + 1] if open_end >= 0 else raw
    class_match = re.search(r'\bclass\s*=\s*(["\'])(.*?)\1', attrs, re.IGNORECASE)
    if class_match is None:
        return False
    return class_name in class_match.group(2).split()


def transform_node_open(raw: str, transform: Callable[[str], str]) -> str:
    match = FLOAT_NODE_PATTERN.match(raw)
    if match is None:
        return raw
    return f"{transform(match.group('open'))}{match.group('body')}{match.group('close')}"


def strip_node_id_and_add_class(raw: str, class_name: str | None = None) -> str:
    def transform(open_tag: str) -> str:
        open_tag = remove_id_attr(open_tag)
        if class_name is not None:
            open_tag = add_class_attr(open_tag, class_name)
        return open_tag

    return transform_node_open(raw, transform)


def matching_div_close_span(html: str, open_end: int) -> tuple[int, int] | None:
    depth = 1
    for match in DIV_TAG_PATTERN.finditer(html, open_end):
        if match.group(0).lower().startswith("</div"):
            depth -= 1
            if depth == 0:
                return match.start(), match.end()
        else:
            depth += 1
    return None


def node_close_end(fragment: str, tag: str, open_end: int) -> int | None:
    close_match = re.search(rf"</{re.escape(tag)}\s*>", fragment[open_end:], re.IGNORECASE)
    if close_match is None:
        return None
    return open_end + close_match.end()

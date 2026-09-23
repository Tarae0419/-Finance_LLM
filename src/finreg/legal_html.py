"""Parse official legal HTML while retaining raw character ranges and hierarchy."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    start: int
    end: int = 0
    children: list["Node | str"] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in {"script", "style", "input", "img"}:
            return ""
        parts = [child.text() if isinstance(child, Node) else child for child in self.children]
        result = "".join(parts)
        return result + ("\n" if self.tag in {"p", "div", "br", "tr"} else " ")


class LegalHTMLParser(HTMLParser):
    def __init__(self, raw: str):
        super().__init__(convert_charrefs=True)
        self.raw = raw
        self.line_offsets = [0]
        for match in re.finditer("\n", raw):
            self.line_offsets.append(match.end())
        self.root = Node("root", {}, 0, len(raw))
        self.stack = [self.root]
        self.feed(raw)
        self.close()

    def character_offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self.character_offset()
        node = Node(tag, {key: value or "" for key, value in attrs}, start)
        self.stack[-1].children.append(node)
        if tag in VOID_TAGS:
            node.end = start + len(self.get_starttag_text())
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack[-1].end = self.character_offset() + len(self.get_starttag_text())
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                end = self.raw.find(">", self.character_offset()) + 1
                for node in self.stack[index:]:
                    node.end = end
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def normalize_text(node: Node) -> str:
    return re.sub(r"\s+", " ", node.text()).strip()


def extract_provisions(raw: str) -> list[dict]:
    """Only recognized article/annex blocks become provisions; no date inference."""
    root = LegalHTMLParser(raw).root
    nodes = list(root.walk())
    blocks = [node for node in nodes if "lawcon" in node.attrs.get("class", "").split()]
    provisions = []

    def add(node, kind, article=None, paragraph=None, item=None, parent=None):
        if node.end <= node.start:
            raise ValueError("Unclosed legal content block")
        record = {
            "local_id": f"{kind}:{node.start}",
            "kind": kind,
            "article": article,
            "paragraph": paragraph,
            "item": item,
            "parent_local_id": parent,
            "text": normalize_text(node),
            "source_offsets": {
                "format": "html",
                "unit": "unicode_codepoint",
                "start": node.start,
                "end": node.end,
            },
            "references": [
                {"text": normalize_text(link), "target_locator": link.attrs["onclick"]}
                for link in node.walk()
                if link.tag == "a"
                and "onclick" in link.attrs
                and "link" in link.attrs.get("class", "").split()
            ],
        }
        provisions.append(record)
        return record["local_id"]

    for block in blocks:
        text = normalize_text(block)
        match = re.match(r"(제\d+조(?:의\d+)?)\s*\(", text)
        if not match:
            raise ValueError("Unrecognized article heading; manual review is required")
        article = match.group(1)
        article_id = add(block, "article", article=article)
        paragraph_id = article_id
        item_id = article_id
        paragraph = None
        item = None
        for child in block.children:
            if not isinstance(child, Node) or child.tag != "p":
                continue
            value = normalize_text(child)
            if not value:
                continue
            circled = (
                re.search(r"[①-⑳]", value)
                if value.startswith(article)
                else re.match(r"[①-⑳]", value)
            )
            number = re.match(r"(\d+)\.", value)
            subitem = re.match(r"([가-힣])\.", value)
            if circled:
                paragraph = str(ord(circled.group()) - ord("①") + 1)
                item = None
                paragraph_id = add(child, "paragraph", article, paragraph, parent=article_id)
                item_id = paragraph_id
            elif number:
                item = number.group(1)
                item_id = add(child, "item", article, paragraph, item, paragraph_id)
            elif subitem:
                add(
                    child,
                    "subitem",
                    article,
                    paragraph,
                    f"{item or ''}:{subitem.group(1)}",
                    item_id,
                )
            elif not value.startswith(article):
                # Retain editorial notes and unclassified continuations without inventing numbering.
                add(child, "continuation", article, paragraph, item, item_id)
    annex = next((node for node in nodes if node.attrs.get("id") == "arDivArea"), None)
    if annex is not None and normalize_text(annex):
        add(annex, "supplementary")
    if not blocks:
        raise ValueError("No official article blocks found; refusing to ingest a viewer shell")
    return provisions

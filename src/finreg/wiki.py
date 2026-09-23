"""Read-only checks for the project wiki, never a legal-evidence retriever."""

import hashlib
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

# Explicit inputs: never discover sources by walking datasets or the whole repository.
ALLOWED_SOURCES = frozenset(
    {
        "docs/FinReg_LLM_PRD_v1.md",
        "docs/FinReg_Sprint_Plan_v1.md",
        "docs/decisions.md",
        "docs/data_model.md",
        "docs/corpus_status.md",
        "docs/review_policy.md",
        "docs/llm_wiki.md",
        "docs/wiki_retrieval.md",
        "docs/review_workbench.md",
    }
)
SUPPORT_PAGES = frozenset({"wiki/index.md", "wiki/log.md", "wiki/AGENTS.md"})
INLINE_LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^\s)]+)\)")


class WikiRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WikiSource(WikiRecord):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WikiPage(WikiRecord):
    path: str = Field(pattern=r"^wiki/concepts/[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
    sources: list[str] = Field(min_length=1)


class WikiManifest(WikiRecord):
    schema_version: Literal[1]
    scope: Literal["project-knowledge"]
    hash_format: Literal["sha256-utf8-lf"]
    sources: list[WikiSource] = Field(min_length=1)
    pages: list[WikiPage] = Field(min_length=1)


def _file(root: Path, relative: str) -> Path:
    candidate = root / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved != candidate:
        raise ValueError("path escape or redirected file")
    # Also reject links to the same lexical path on platforms supporting junctions.
    if any(part.is_symlink() or part.is_junction() for part in (candidate, *candidate.parents)):
        raise ValueError("symlinks and junctions are not wiki inputs")
    if not candidate.is_file():
        raise ValueError("missing file")
    return candidate


def text_hash(path: Path) -> str:
    """Hash UTF-8 text with universal newlines; raw legal bytes use a different contract."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _manifest(root: Path) -> WikiManifest:
    manifest = WikiManifest.model_validate_json(
        _file(root, "wiki/manifest.json").read_text(encoding="utf-8")
    )
    source_paths = [source.path for source in manifest.sources]
    page_paths = [page.path for page in manifest.pages]
    if len(set(source_paths)) != len(source_paths) or len(set(page_paths)) != len(page_paths):
        raise ValueError("duplicate source or page registration")
    if set(source_paths) - ALLOWED_SOURCES:
        raise ValueError("source outside the project-document allowlist")
    for page in manifest.pages:
        if set(page.sources) - set(source_paths) or len(page.sources) != len(set(page.sources)):
            raise ValueError("unregistered or duplicate page source")
    return manifest


def source_hashes(root: Path) -> dict[str, str]:
    """Display candidate hashes for manual reconciliation; never rewrite the manifest."""
    root = root.resolve()
    manifest = _manifest(root)
    return {source.path: text_hash(_file(root, source.path)) for source in manifest.sources}


def _links(root: Path, relative: str, content: str) -> tuple[set[str], list[str]]:
    # The wiki convention supports inline file links outside Markdown code examples.
    content = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", content)
    content = re.sub(r"(`+).*?\1", "", content)
    targets: set[str] = set()
    issues: list[str] = []
    for match in INLINE_LINK.finditer(content):
        try:
            url = urlsplit(match[1].strip("<>"))
            if url.scheme in {"http", "https"} and url.netloc:
                continue  # No network calls and no claim that external links were checked.
            if url.scheme or url.netloc:
                raise ValueError("unsupported link scheme")
            path = unquote(url.path)
            if not path:
                continue  # Heading fragments are outside the file-existence check.
            if "\\" in path or path.startswith("/") or ":" in path:
                raise ValueError("use relative POSIX file links")
            target = (root / relative).parent / path
            # Normalize .. lexically, then let _file reject redirection at the target.
            normalized = Path(os.path.abspath(target)).relative_to(root).as_posix()
            _file(root, normalized)
            targets.add(normalized)
        except (OSError, ValueError):
            issues.append(f"{relative}: broken or unsafe file link")
    return targets, issues


def _wiki_pages(directory: Path) -> set[Path]:
    pages: set[Path] = set()
    for path in directory.iterdir():
        if path.is_symlink() or path.is_junction():
            raise ValueError("redirected wiki entry")
        if path.is_dir():
            pages.update(_wiki_pages(path))
        elif path.suffix == ".md":
            pages.add(path)
    return pages


def check_wiki(root: Path) -> list[str]:
    """Return structural/staleness issues without changing files or judging legal meaning."""
    root = root.resolve()
    try:
        manifest = _manifest(root)
    except (OSError, ValueError):
        return ["wiki/manifest.json: invalid, missing, unsafe, or disallowed registration"]

    issues: list[str] = []
    registered_sources = {source.path for source in manifest.sources}
    for source in manifest.sources:
        try:
            current = text_hash(_file(root, source.path))
        except (OSError, ValueError):
            issues.append(f"{source.path}: missing, unreadable, or unsafe source")
            continue
        if current != source.sha256:
            affected = ", ".join(
                page.path for page in manifest.pages if source.path in page.sources
            )
            issues.append(f"{source.path}: stale source; reconcile {affected}")

    page_paths = {page.path for page in manifest.pages}
    known_pages = SUPPORT_PAGES | page_paths
    try:
        actual_pages = {path.relative_to(root).as_posix() for path in _wiki_pages(root / "wiki")}
    except (OSError, ValueError):
        issues.append("wiki: unreadable or redirected entry")
        actual_pages = set()
    for path in sorted(actual_pages - known_pages):
        issues.append(f"{path}: unregistered wiki page")

    links: dict[str, set[str]] = {}
    for relative in sorted(known_pages):
        try:
            content = _file(root, relative).read_text(encoding="utf-8")
        except (OSError, ValueError):
            issues.append(f"{relative}: missing, unreadable, or unsafe page")
            continue
        if not content.strip():
            issues.append(f"{relative}: empty page")
        links[relative], link_issues = _links(root, relative, content)
        issues.extend(link_issues)
        # The parent policy is a navigation link on the wiki's instruction page only.
        allowed = known_pages | registered_sources
        if relative == "wiki/AGENTS.md":
            allowed = allowed | {"AGENTS.md"}
        if links[relative] - allowed:
            issues.append(f"{relative}: link outside registered wiki/source files")

    for page in manifest.pages:
        if page.path not in links:
            continue
        linked_sources = links[page.path] & registered_sources
        if linked_sources != set(page.sources):
            issues.append(f"{page.path}: source links and declared dependencies differ")
        if page.path not in links.get("wiki/index.md", set()):
            issues.append(f"{page.path}: missing from wiki/index.md")
    return issues

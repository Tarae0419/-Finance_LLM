import json
import subprocess
import sys
from pathlib import Path

import pytest

from finreg.wiki import check_wiki, source_hashes, text_hash


@pytest.fixture
def wiki(tmp_path):
    (tmp_path / "docs").mkdir()
    source = tmp_path / "docs/data_model.md"
    source.write_text("# Fictional project document\nOriginal text.\n", encoding="utf-8")
    (tmp_path / "wiki/concepts").mkdir(parents=True)
    (tmp_path / "wiki/concepts/example.md").write_text(
        "# Example\n[Source](../../docs/data_model.md)\n[Index](../index.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "wiki/index.md").write_text("[Example](concepts/example.md)\n", encoding="utf-8")
    for path in ("wiki/log.md", "wiki/AGENTS.md"):
        (tmp_path / path).write_text("# Project documentation\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "scope": "project-knowledge",
        "hash_format": "sha256-utf8-lf",
        "sources": [{"path": "docs/data_model.md", "sha256": text_hash(source)}],
        "pages": [{"path": "wiki/concepts/example.md", "sources": ["docs/data_model.md"]}],
    }
    (tmp_path / "wiki/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def edit_manifest(root, edit):
    path = root / "wiki/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_stale_sources_report_affected_page_without_rewriting(wiki):
    assert check_wiki(wiki) == []
    manifest = (wiki / "wiki/manifest.json").read_bytes()
    (wiki / "docs/data_model.md").write_text("Revised project contract", encoding="utf-8")
    assert any(
        "stale source; reconcile wiki/concepts/example.md" in issue for issue in check_wiki(wiki)
    )
    assert source_hashes(wiki)["docs/data_model.md"] == text_hash(wiki / "docs/data_model.md")
    assert (wiki / "wiki/manifest.json").read_bytes() == manifest
    assert check_wiki(wiki)  # Displaying new hashes does not acknowledge a change.


def test_newline_conversion_does_not_mark_source_stale(wiki):
    source = wiki / "docs/data_model.md"
    raw = source.read_bytes().replace(b"\r\n", b"\n")
    for newline in (b"\n", b"\r\n", b"\r"):
        source.write_bytes(raw.replace(b"\n", newline))
        assert check_wiki(wiki) == []


@pytest.mark.parametrize(
    "source", ["data/sealed/answers.json", "../outside.md", "docs/../data/holdout/answers.json"]
)
def test_disallowed_source_is_rejected_before_reading(wiki, source, monkeypatch):
    edit_manifest(wiki, lambda m: m["sources"][0].update(path=source))
    original_read = Path.read_text
    reads = []

    def guarded_read(path, *args, **kwargs):
        reads.append(path.relative_to(wiki).as_posix())
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert check_wiki(wiki)
    assert reads == ["wiki/manifest.json"]


@pytest.mark.parametrize(
    "link",
    ["missing.md", "../../../outside.md", "../../data/sealed/answers.json", "file:///etc/passwd"],
)
def test_broken_or_unsafe_link_is_rejected(wiki, link):
    page = wiki / "wiki/concepts/example.md"
    with page.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[Bad link]({link})\n")
    assert any("file link" in issue for issue in check_wiki(wiki))


def test_missing_dependencies_and_index_entries_are_reported(wiki):
    page = wiki / "wiki/concepts/example.md"
    page.write_text("# Missing source\n", encoding="utf-8")
    (wiki / "wiki/index.md").write_text("# Empty index\n", encoding="utf-8")
    issues = check_wiki(wiki)
    assert any("dependencies differ" in issue for issue in issues)
    assert any("missing from wiki/index.md" in issue for issue in issues)


def test_unregistered_pages_and_undeclared_source_links_are_reported(wiki):
    (wiki / "wiki/concepts/orphan.md").write_text("# Orphan\n", encoding="utf-8")
    (wiki / "docs/review_policy.md").write_text("# Another source\n", encoding="utf-8")
    with (wiki / "wiki/concepts/example.md").open("a", encoding="utf-8") as stream:
        stream.write("[Other](../../docs/review_policy.md)\n")
    issues = check_wiki(wiki)
    assert any("unregistered wiki page" in issue for issue in issues)
    assert any("outside registered" in issue for issue in issues)


def test_duplicate_registration_and_malformed_json_fail_closed(wiki):
    edit_manifest(wiki, lambda m: m["sources"].append(m["sources"][0]))
    assert check_wiki(wiki)
    (wiki / "wiki/manifest.json").write_text("{broken", encoding="utf-8")
    assert check_wiki(wiki)


def test_redirected_allowlisted_file_is_not_read(wiki, monkeypatch):
    original_resolve = Path.resolve
    source = wiki / "docs/data_model.md"

    def redirected(path, *args, **kwargs):
        if path == source:
            return wiki / "data/sealed/answers.json"
        return original_resolve(path, *args, **kwargs)

    # Simulates the OS resolution of a symlink without requiring Windows symlink privileges.
    monkeypatch.setattr(Path, "resolve", redirected)
    assert any("unsafe source" in issue for issue in check_wiki(wiki))


def test_code_examples_are_not_file_links(wiki):
    with (wiki / "wiki/concepts/example.md").open("a", encoding="utf-8") as stream:
        stream.write("\n`[example](missing.md)`\n\n```markdown\n[example](missing.md)\n```\n")
    assert check_wiki(wiki) == []


def test_cli_exit_code_distinguishes_clean_and_stale(wiki):
    script = Path(__file__).resolve().parents[1] / "scripts/check_wiki.py"
    command = [sys.executable, str(script), "--root", str(wiki)]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    (wiki / "docs/data_model.md").write_text("Changed", encoding="utf-8")
    result = subprocess.run(command, capture_output=True, check=False)
    assert result.returncode == 1
    assert b"stale source" in result.stdout

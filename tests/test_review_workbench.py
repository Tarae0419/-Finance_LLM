import hashlib
import json
from html.parser import HTMLParser

import pytest
from psycopg.types.json import Jsonb
from test_corpus import db as db
from test_dataset import example as example

from finreg.corpus import store_raw
from finreg.dataset import register_draft
from finreg.review_workbench import collect_bundle, render_workbench, write_workbench

pytestmark = pytest.mark.integration


def test_packet_accepts_pending_corpus_but_never_reads_test_payloads(db, example, tmp_path):
    register_draft(db, example)
    register_draft(
        db,
        example.model_copy(
            update={
                "example_id": "holdout-only",
                "group_id": "holdout-group",
                "split": "test",
                "question": "DO_NOT_EXPORT_THIS_HELD_OUT_QUESTION",
            }
        ),
    )
    bundle, assets = collect_bundle(db, tmp_path, [example.dataset_id])
    assert len(bundle["examples"]) == 1
    entry = bundle["examples"][0]
    assert entry["review_status"] == "draft"
    assert entry["template"]["human_attested"] is False
    assert entry["template"]["review_date"] is None
    assert entry["evidence"][0]["source"]["temporal_review_status"] == "unreviewed"
    assert entry["evidence"][0]["quote_matches"]
    assert "DO_NOT_EXPORT" not in json.dumps(bundle)
    assert not assets
    assert db.execute("SELECT count(*) FROM review_log").fetchone()[0] == 0
    assert db.execute("SELECT validation_status FROM corpus_snapshot").fetchone()[0] == "pending"


def test_source_bytes_and_stored_text_must_match(db, example, tmp_path):
    register_draft(db, example)
    path = db.execute("SELECT raw_path FROM document_version").fetchone()[0]
    (tmp_path / path).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        collect_bundle(db, tmp_path, [example.dataset_id])


def test_changed_stored_provision_is_rejected(db, example, tmp_path):
    register_draft(db, example)
    db.execute("UPDATE provision SET text='changed without source revision'")
    with pytest.raises(ValueError, match="original source"):
        collect_bundle(db, tmp_path, [example.dataset_id])


def test_only_current_revision_and_matching_template_are_exported(db, example, tmp_path):
    register_draft(db, example)
    first, _ = collect_bundle(db, tmp_path, [example.dataset_id])
    revised = example.model_copy(update={"question": "가상 질문 수정본"})
    register_draft(db, revised)
    second, _ = collect_bundle(db, tmp_path, [example.dataset_id])
    assert len(second["examples"]) == 1
    assert second["examples"][0]["template"]["revision"] == 2
    assert first["bundle_id"] != second["bundle_id"]


def test_script_like_source_content_is_only_data(db, example, tmp_path):
    attack = '</script><img src="bad" onerror="globalThis.injected=true"><script>'
    register_draft(db, example.model_copy(update={"question": attack}))
    bundle, _ = collect_bundle(db, tmp_path, [example.dataset_id])
    html = render_workbench(bundle)

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = 0
            self.images = 0

        def handle_starttag(self, tag, attrs):
            self.scripts += tag == "script"
            self.images += tag == "img"

    tags = Tags()
    tags.feed(html)
    assert tags.scripts == 2 and tags.images == 0
    assert attack not in html
    assert "\\u003c/script\\u003e" in html
    assert "connect-src 'none'" in html


def test_pdf_integrity_and_missing_attachment_status(db, example, tmp_path):
    register_draft(db, example)
    raw = b"%PDF-1.4\nSynthetic attachment for tests only"
    path, digest = store_raw(tmp_path, raw, "pdf")
    attachment = {
        "title": "가상 별표",
        "source_url": "https://www.law.go.kr/fixture",
        "collection_status": "collected",
        "extraction_status": "pending",
        "raw_path": path,
        "content_hash": digest,
    }
    db.execute("UPDATE document_version SET attachments=%s", (Jsonb([attachment]),))
    bundle, assets = collect_bundle(db, tmp_path, [example.dataset_id])
    assert list(assets.values()) == [raw]
    assert bundle["examples"][0]["evidence"][0]["source"]["attachments"][0]["local_path"]
    (tmp_path / path).write_bytes(b"not the original")
    bundle, assets = collect_bundle(db, tmp_path, [example.dataset_id])
    assert not assets
    assert bundle["examples"][0]["evidence"][0]["source"]["attachments"][0]["local_status"]


def test_output_is_confined_immutable_and_manifest_hashes_match(db, example, tmp_path):
    register_draft(db, example)
    bundle, assets = collect_bundle(db, tmp_path, [example.dataset_id])
    with pytest.raises(ValueError, match="artifacts/review-workbench"):
        write_workbench(tmp_path, tmp_path / "data/sealed/bundle", bundle, assets)
    output = tmp_path / "artifacts/review-workbench/fixture"
    summary = write_workbench(tmp_path, output, bundle, assets)
    for path, digest in summary["files"].items():
        assert hashlib.sha256((output / path).read_bytes()).hexdigest() == digest
    before = (output / "index.html").read_bytes()
    with pytest.raises(FileExistsError):
        write_workbench(tmp_path, output, bundle, assets)
    assert (output / "index.html").read_bytes() == before


def test_empty_or_test_only_dataset_is_not_exported(db, example, tmp_path):
    register_draft(db, example.model_copy(update={"split": "test"}))
    with pytest.raises(ValueError, match="No development"):
        collect_bundle(db, tmp_path, [example.dataset_id])

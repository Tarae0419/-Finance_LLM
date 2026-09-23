import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from finreg.config import Settings
from finreg.corpus import activate_snapshot, create_snapshot, ingest_source, store_raw
from finreg.legal_html import extract_provisions
from finreg.migrate import connect_database, migrate

# Fictional rule used only to test preservation and state handling, never legal accuracy.
FIXTURE = """<div class="lawcon">
<p><span>제1조(가상 설명)</span> ① 설명하여야 한다. 다만, 가상 예외는 제외한다.</p>
<p>1. 수수료는 1%를 넘지 않는다.</p><p>가. 추가 비용은 없다.</p>
<p>② <a class="link" onclick="unresolved-reference">다른 가상 규정</a>을 확인한다.</p>
</div><div id="arDivArea"><p>가상 부칙: 이전 거래에는 적용하지 않는다.</p></div>"""


def test_parser_keeps_exceptions_units_hierarchy_and_raw_ranges():
    records = extract_provisions(FIXTURE)
    article = records[0]
    assert "다만" in article["text"] and "1%" in article["text"]
    assert "넘지 않는다" in article["text"]
    paragraph = next(row for row in records if row["kind"] == "paragraph")
    item = next(row for row in records if row["kind"] == "item")
    assert paragraph["parent_local_id"] == article["local_id"]
    assert item["parent_local_id"] == paragraph["local_id"]
    assert records[-1]["kind"] == "supplementary"
    for row in records:
        offsets = row["source_offsets"]
        assert FIXTURE[offsets["start"] : offsets["end"]].startswith("<")
        assert offsets["end"] <= len(FIXTURE)
    assert article["references"][0]["target_locator"] == "unresolved-reference"


def test_parser_refuses_a_viewer_shell():
    with pytest.raises(ValueError, match="No official article blocks"):
        extract_provisions('<html><iframe src="body"></iframe></html>')


@pytest.fixture
def db():
    url = os.environ.get("FINREG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration URL is not configured")
    with connect_database(Settings(_env_file=None, database_url=SecretStr(url))) as connection:
        with connection.transaction(force_rollback=True):
            name = "test_" + uuid4().hex
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(name)))
            migrate(connection, Path("migrations"))
            yield connection


def source_fixture(tmp_path):
    path, digest = store_raw(tmp_path, FIXTURE.encode(), "html")
    return {
        "official_id": "fictional-test-only",
        "document_type": "law",
        "title": "가상 테스트 규정",
        "authority": "test fixture",
        "source_url": "https://example.invalid/fictional",
        "official_version_id": "test-v1",
        "promulgated_at": "2000-01-01",
        "effective_from": "2000-01-02",
        "collected_at": datetime.now(UTC).isoformat(),
        "raw_path": path,
        "content_hash": digest,
    }


@pytest.mark.integration
def test_ingestion_is_idempotent_and_never_infers_temporal_review(db, tmp_path):
    source = source_fixture(tmp_path)
    first = ingest_source(db, tmp_path, source)
    assert ingest_source(db, tmp_path, source) == first
    assert db.execute("SELECT count(*) FROM document_version").fetchone()[0] == 1
    rows = db.execute("SELECT valid_from, temporal_review_status FROM applicability").fetchall()
    assert rows and all(row == (None, "unreviewed") for row in rows)
    assert (
        db.execute("SELECT count(*) FROM provision_relation WHERE to_id IS NULL").fetchone()[0] > 0
    )
    snapshot = create_snapshot(db, [first])
    with pytest.raises(ValueError, match="has not passed review"):
        activate_snapshot(db, snapshot)
    assert db.execute("SELECT count(*) FROM corpus_state").fetchone()[0] == 0


@pytest.mark.integration
def test_corrupt_raw_file_is_rejected_before_db_write(db, tmp_path):
    source = source_fixture(tmp_path)
    (tmp_path / source["raw_path"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        ingest_source(db, tmp_path, source)
    assert db.execute("SELECT count(*) FROM document_version").fetchone()[0] == 0


@pytest.mark.integration
def test_database_rejects_temporal_verification_without_human_record(db, tmp_path):
    ingest_source(db, tmp_path, source_fixture(tmp_path))
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("UPDATE applicability SET temporal_review_status='verified'")


@pytest.mark.integration
def test_migrations_can_be_replayed_without_changing_schema(db):
    assert migrate(db, Path("migrations")) == []


@pytest.mark.integration
def test_failed_activation_keeps_previous_snapshot(db, tmp_path):
    version = ingest_source(db, tmp_path, source_fixture(tmp_path))
    previous = create_snapshot(db, [version])
    ids = [str(row[0]) for row in db.execute("SELECT provision_id FROM provision").fetchall()]
    # Synthetic review state for transaction testing, never applied to the real corpus.
    db.execute(
        "UPDATE applicability SET valid_from='2000-01-02', valid_to_exclusive='2001-01-01', "
        "temporal_review_status='verified', reviewer='test fixture', reviewed_at=now()"
    )
    coverage = {
        "supported": True,
        "provision_ids": ids,
        "valid_from": "2000-01-02",
        "valid_to_exclusive": "2001-01-01",
    }
    db.execute(
        "UPDATE corpus_snapshot SET validation_status='verified', coverage=%s",
        (Jsonb(coverage),),
    )
    activate_snapshot(db, previous)
    candidate = uuid4()
    coverage["valid_to_exclusive"] = "2001-01-02"
    db.execute(
        "INSERT INTO corpus_snapshot (snapshot_id,validation_status,coverage) "
        "VALUES (%s,'verified',%s)",
        (candidate, Jsonb(coverage)),
    )
    db.execute("INSERT INTO snapshot_version VALUES (%s,%s)", (candidate, version))
    with pytest.raises(ValueError, match="missing or temporally unreviewed"):
        activate_snapshot(db, candidate)
    assert db.execute("SELECT snapshot_id FROM corpus_state").fetchone()[0] == previous
    assert (
        db.execute(
            "SELECT activated_at FROM corpus_snapshot WHERE snapshot_id=%s",
            (candidate,),
        ).fetchone()[0]
        is None
    )

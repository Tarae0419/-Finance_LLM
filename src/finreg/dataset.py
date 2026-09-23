"""Draft revision storage and explicit human review imports; no automatic legal grading."""

import hashlib
import json
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

CheckResult = Literal["pending", "pass", "fail", "uncertain"]
CHECKS = ("existence", "meaning", "temporal", "completeness")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(Record):
    provision_id: UUID
    quote: str = Field(min_length=1)


class Example(Record):
    example_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    split: Literal["train", "dev", "test"]
    snapshot_id: UUID
    question: str = Field(min_length=1)
    as_of_date: date
    facts: dict
    category: str = Field(min_length=1)
    evidence: list[Evidence]
    expected_answer: str = Field(min_length=1)
    expected_status: Literal[
        "answered", "needs_clarification", "insufficient_evidence", "out_of_scope", "system_error"
    ]
    review_status: Literal["draft"] = "draft"
    limitations: list[str]

    @field_validator("evidence")
    @classmethod
    def unique_evidence(cls, value):
        if len({row.provision_id for row in value}) != len(value):
            raise ValueError("Duplicate evidence IDs")
        return value


class Check(Record):
    result: CheckResult
    reason: str = Field(min_length=1)


class CrossCheck(Record):
    source_url: str
    locator: str = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def official_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {
            "fsc.go.kr",
            "www.fsc.go.kr",
            "fss.or.kr",
            "www.fss.or.kr",
        }:
            raise ValueError("Cross-check must identify an official FSC/FSS source")
        return value


class HumanReview(Record):
    example_id: str
    revision: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provision_ids: list[UUID]
    reviewer: str = Field(min_length=1)
    reviewer_role: Literal["developer", "external"]
    reviewer_role_detail: str = Field(min_length=1)
    review_date: datetime
    method: str = Field(min_length=1)
    human_attested: Literal[True]
    existence: Check
    meaning: Check
    temporal: Check
    completeness: Check
    official_cross_checks: list[CrossCheck] = Field(default_factory=list)
    excluded_reason: str | None = Field(default=None, min_length=1)
    duration_seconds: int = Field(ge=0)

    @field_validator("review_date")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Review time must include timezone")
        return value


def content_hash(example: Example) -> str:
    raw = json.dumps(example.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def review_template(example: Example, revision: int) -> dict:
    return {
        "example_id": example.example_id,
        "revision": revision,
        "content_hash": content_hash(example),
        "provision_ids": [str(item.provision_id) for item in example.evidence],
        "reviewer": None,
        "reviewer_role": None,
        "reviewer_role_detail": None,
        "review_date": None,
        "method": None,
        "human_attested": False,
        **{name: {"result": "pending", "reason": ""} for name in CHECKS},
        "official_cross_checks": [],
        "excluded_reason": None,
        "duration_seconds": None,
    }


def validate_evidence(connection, example: Example):
    for item in example.evidence:
        row = connection.execute(
            "SELECT p.text FROM provision p JOIN snapshot_version v USING(version_id) "
            "WHERE p.provision_id=%s AND v.snapshot_id=%s",
            (item.provision_id, example.snapshot_id),
        ).fetchone()
        if not row or item.quote not in row[0]:
            raise ValueError("Evidence ID/snapshot/quote mismatch")


def register_draft(connection, example: Example) -> int:
    """Identical imports are idempotent; changes create an unreviewed revision."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(72401003)")
        validate_evidence(connection, example)
        connection.execute(
            "INSERT INTO dataset_group VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (example.group_id, example.split),
        )
        split = connection.execute(
            "SELECT split FROM dataset_group WHERE group_id=%s", (example.group_id,)
        ).fetchone()[0]
        if split != example.split:
            raise ValueError("Related group cannot cross dataset splits")
        latest = connection.execute(
            "SELECT revision,content_hash,group_id,dataset_id FROM example_revision "
            "WHERE example_id=%s ORDER BY revision DESC LIMIT 1",
            (example.example_id,),
        ).fetchone()
        digest = content_hash(example)
        if latest:
            if latest[2:] != (example.group_id, example.dataset_id):
                raise ValueError("Example identity cannot change group or dataset")
            if latest[1] == digest:
                return latest[0]
        revision = latest[0] + 1 if latest else 1
        connection.execute(
            "INSERT INTO example_revision "
            "(example_id,revision,dataset_id,group_id,snapshot_id,content_hash,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                example.example_id,
                revision,
                example.dataset_id,
                example.group_id,
                example.snapshot_id,
                digest,
                Jsonb(example.model_dump(mode="json")),
            ),
        )
        for item in example.evidence:
            connection.execute(
                "INSERT INTO example_evidence VALUES (%s,%s,%s,%s)",
                (example.example_id, revision, item.provision_id, item.quote),
            )
        return revision


def record_review(connection, review: HumanReview) -> str:
    """Import a human's checklist. The attestation is not identity authentication."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(72401003)")
        row = connection.execute(
            "SELECT revision,content_hash,payload FROM current_example WHERE example_id=%s",
            (review.example_id,),
        ).fetchone()
        if not row or row[:2] != (review.revision, review.content_hash):
            raise ValueError("Stale review: exact current revision and hash required")
        example = Example.model_validate(row[2])
        expected = {item.provision_id for item in example.evidence}
        if set(review.provision_ids) != expected or len(review.provision_ids) != len(expected):
            raise ValueError("Review must cover every evidence ID exactly once")
        if review.existence.result == "pass":
            validate_evidence(connection, example)
        checks = [getattr(review, name).result for name in CHECKS]
        reasons = {name: getattr(review, name).reason for name in CHECKS}
        return connection.execute(
            "INSERT INTO review_log (example_id,revision,content_hash,reviewer,reviewer_role,"
            "reviewer_role_detail,review_date,method,human_attested,existence,meaning,temporal,"
            "completeness,reasons,official_cross_checks,excluded_reason,duration_seconds) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING result",
            (
                review.example_id,
                review.revision,
                review.content_hash,
                review.reviewer,
                review.reviewer_role,
                review.reviewer_role_detail,
                review.review_date,
                review.method,
                review.human_attested,
                *checks,
                Jsonb(reasons),
                Jsonb([entry.model_dump() for entry in review.official_cross_checks]),
                review.excluded_reason,
                review.duration_seconds,
            ),
        ).fetchone()[0]


def reviewed_examples(connection, dataset_id: str, split: str) -> list[dict]:
    """Development/training export has no route to test contents."""
    if split not in {"train", "dev"}:
        raise ValueError("Test data is not accessible through development export")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT example_id,revision,content_hash,payload,review_status,external_review "
            "FROM current_example WHERE dataset_id=%s AND split=%s AND review_status='reviewed' "
            "ORDER BY example_id",
            (dataset_id, split),
        )
        rows = cursor.fetchall()
    for row in rows:
        row["payload"]["review_status"] = row["review_status"]
    return rows

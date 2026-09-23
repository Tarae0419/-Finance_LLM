"""Local, transactional ingestion. Collected material is not verified legal coverage."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from finreg.legal_html import LegalHTMLParser, extract_provisions, normalize_text


def official_bytes(url: str, limit: int = 16 * 1024 * 1024) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.law.go.kr", "law.go.kr"}:
        raise ValueError("This collector only accepts official law.go.kr HTTPS URLs")
    with urlopen(url, timeout=30) as response:
        final = urlsplit(response.url)
        if final.scheme != "https" or final.hostname not in {"www.law.go.kr", "law.go.kr"}:
            raise ValueError("Unexpected source redirect")
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Source exceeds collection size limit")
    return raw


def store_raw(root: Path, raw: bytes, extension: str) -> tuple[str, str]:
    digest = hashlib.sha256(raw).hexdigest()
    relative = Path("data/raw") / f"{digest}.{extension}"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Stored source hash collision or corruption")
    else:
        with path.open("xb") as out:
            out.write(raw)
    return relative.as_posix(), digest


def collect_source(root: Path, source: dict) -> dict:
    raw = official_bytes(source["download_url"])
    text = raw.decode("utf-8")
    tree = LegalHTMLParser(text).root
    fields = {n.attrs.get("id"): n.attrs.get("value") for n in tree.walk() if n.tag == "input"}
    actual_id = fields.get("lsId") or fields.get("admRulId")
    if actual_id != source["official_id"]:
        raise ValueError("Official document ID does not match the requested source")
    actual_version = fields.get("lsiSeq") or fields.get("admRulSeq")
    if actual_version and actual_version != source["official_version_id"]:
        raise ValueError("Official version ID does not match the requested source")
    provisions = extract_provisions(text)
    raw_path, digest = store_raw(root, raw, "html")
    attachments = []
    seen = set()
    for node in tree.walk():
        href = node.attrs.get("href", "")
        if node.tag != "a" or "flDownload.do" not in href:
            continue
        if not any("PDF" in image.attrs.get("alt", "") for image in node.walk()):
            continue
        url = urljoin(source["download_url"], href)
        if url in seen:
            continue
        seen.add(url)
        name = parse_qs(urlsplit(url).query).get("flNm", ["appendix"])[0]
        attachment = {"title": name, "source_url": url, "extraction_status": "pending"}
        try:
            blob = official_bytes(url)
            if not blob.startswith(b"%PDF"):
                raise ValueError("Attachment response is not a PDF")
            path, content_hash = store_raw(root, blob, "pdf")
            attachment.update(
                raw_path=path, content_hash=content_hash, collection_status="collected"
            )
        except (OSError, ValueError) as error:
            attachment.update(collection_status="failed", error_type=type(error).__name__)
        attachments.append(attachment)
    header = next(
        (
            normalize_text(n)
            for n in tree.walk()
            if {"ct_sub", "subtit1"}.intersection(n.attrs.get("class", "").split())
        ),
        "",
    )
    dates = re.fullmatch(
        r"\[시행 (\d+)\.\s*(\d+)\.\s*(\d+)\.\]\s*"
        r"\[[^,]+,\s*(\d+)\.\s*(\d+)\.\s*(\d+)\.,[^\]]+\]",
        header,
    )
    if not dates:
        raise ValueError("Unrecognized official date header")
    parts = [int(value) for value in dates.groups()]
    if date(*parts[:3]) != date.fromisoformat(source["effective_from"]):
        raise ValueError("Effective-date header does not match the pinned source configuration")
    if date(*parts[3:]) != date.fromisoformat(source["promulgated_at"]):
        raise ValueError("Promulgation header does not match the pinned source configuration")
    return {
        **source,
        "collected_at": datetime.now(UTC).isoformat(),
        "content_hash": digest,
        "raw_path": raw_path,
        "attachments": attachments,
        "observed_header": header,
        "provision_count": len(provisions),
        "temporal_review_status": "unreviewed",
        "limitations": [
            "조문별 시행·경과조치 미검수",
            "참조 관계 미해결",
            "별표 표·단위·조건 추출 미검수",
            "공식 FAQ·해석자료 미확보",
        ],
    }


def source_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "data/raw").resolve()):
        raise ValueError("Raw source must be inside data/raw")
    return path


def ingest_source(connection, root: Path, source: dict) -> UUID:
    raw = source_path(root, source["raw_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["content_hash"]:
        raise ValueError("Raw source hash mismatch")
    provisions = extract_provisions(raw.decode("utf-8"))
    source_id = uuid5(NAMESPACE_URL, f"finreg:{source['document_type']}:{source['official_id']}")
    version_id = uuid5(source_id, source["official_version_id"] + ":" + source["content_hash"])
    with connection.transaction():
        connection.execute(
            "INSERT INTO source_document VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                source_id,
                source["official_id"],
                source["title"],
                source["document_type"],
                source["authority"],
                source["source_url"],
            ),
        )
        connection.execute(
            "INSERT INTO document_version VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                version_id,
                source_id,
                source["official_version_id"],
                source["promulgated_at"],
                source["effective_from"],
                source["collected_at"],
                source["content_hash"],
                source["raw_path"],
                source["source_url"],
                Jsonb(source.get("attachments", [])),
            ),
        )
        for provision in provisions:
            provision_id = uuid5(version_id, provision["local_id"])
            parent = provision["parent_local_id"]
            connection.execute(
                "INSERT INTO provision VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    provision_id,
                    version_id,
                    provision["kind"],
                    provision["article"],
                    provision["paragraph"],
                    provision["item"],
                    provision["text"],
                    Jsonb(provision["source_offsets"]),
                    uuid5(version_id, parent) if parent else None,
                ),
            )
            # Document-level dates must never silently become reviewed article-level dates.
            connection.execute(
                "INSERT INTO applicability (provision_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (provision_id,),
            )
            for index, reference in enumerate(provision["references"]):
                connection.execute(
                    "INSERT INTO provision_relation "
                    "(relation_id,from_id,relation_type,target_locator) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        uuid5(provision_id, str(index)),
                        provision_id,
                        "reference_candidate",
                        json.dumps(reference, ensure_ascii=False),
                    ),
                )
    return version_id


def create_snapshot(connection, version_ids: list[UUID]) -> UUID:
    if not version_ids:
        raise ValueError("A snapshot needs at least one document version")
    snapshot_id = uuid5(NAMESPACE_URL, "finreg:snapshot:" + ":".join(sorted(map(str, version_ids))))
    with connection.transaction():
        connection.execute(
            "INSERT INTO corpus_snapshot (snapshot_id,coverage) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                snapshot_id,
                Jsonb({"supported": False, "provision_ids": [], "review_status": "pending"}),
            ),
        )
        for version_id in version_ids:
            connection.execute(
                "INSERT INTO snapshot_version VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (snapshot_id, version_id),
            )
    return snapshot_id


def activate_snapshot(connection, snapshot_id: UUID) -> None:
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(72401002)")
        row = connection.execute(
            "SELECT validation_status,coverage FROM corpus_snapshot "
            "WHERE snapshot_id=%s FOR UPDATE",
            (snapshot_id,),
        ).fetchone()
        if not row or row[0] != "verified" or not row[1].get("supported"):
            raise ValueError("Snapshot has not passed review and coverage validation")
        try:
            valid_from = date.fromisoformat(row[1]["valid_from"])
            valid_to = date.fromisoformat(row[1]["valid_to_exclusive"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Snapshot needs an explicitly reviewed coverage period") from None
        if valid_from >= valid_to:
            raise ValueError("Invalid snapshot coverage period")
        ids = [UUID(value) for value in row[1].get("provision_ids", [])]
        if not ids:
            raise ValueError("No verified provisions in snapshot coverage")
        count = connection.execute(
            "SELECT count(*) FROM provision p JOIN applicability a USING(provision_id) "
            "JOIN snapshot_version v USING(version_id) WHERE v.snapshot_id=%s AND "
            "p.provision_id=ANY(%s) AND a.temporal_review_status='verified' "
            "AND a.valid_from <= %s "
            "AND (a.valid_to_exclusive IS NULL OR a.valid_to_exclusive >= %s)",
            (snapshot_id, ids, valid_from, valid_to),
        ).fetchone()[0]
        if count != len(set(ids)):
            raise ValueError("Coverage includes missing or temporally unreviewed provisions")
        connection.execute(
            "UPDATE corpus_snapshot SET activated_at=now(),last_success_at=now() "
            "WHERE snapshot_id=%s",
            (snapshot_id,),
        )
        connection.execute(
            "INSERT INTO corpus_state VALUES (true,%s) ON CONFLICT (singleton) "
            "DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id",
            (snapshot_id,),
        )

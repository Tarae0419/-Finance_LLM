"""Build an offline development-review workspace; never approve or mutate source data."""

import hashlib
import json
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid5

from psycopg.rows import dict_row

from finreg.dataset import Example, content_hash, review_template
from finreg.legal_html import extract_provisions


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _raw(root: Path, relative: str, digest: str) -> bytes:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid source hash")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve() / "data" / "raw"):
        raise ValueError("Source outside data/raw")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Raw source hash mismatch")
    return raw


def collect_bundle(connection, root: Path, dataset_ids: list[str]) -> tuple[dict, dict[str, bytes]]:
    """Caller fixes a repeatable-read, read-only transaction. No test payload query exists."""
    if not dataset_ids:
        raise ValueError("Choose development datasets explicitly")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT example_id,revision,content_hash,payload,review_status FROM current_example "
            "WHERE split='dev' AND dataset_id=ANY(%s) ORDER BY example_id",
            (dataset_ids,),
        )
        rows = cursor.fetchall()
    if not rows:
        raise ValueError("No development examples in the selected datasets")
    assets, versions, provisions = {}, {}, {}

    def provision(snapshot, identifier):
        cache_key = (snapshot, identifier)
        if cache_key in provisions:
            return provisions[cache_key]
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT p.*,d.title,d.document_type,v.content_hash AS raw_sha256,"
                "v.raw_path,v.source_url,v.promulgated_at,v.effective_from,v.collected_at,"
                "v.attachments,a.valid_from,a.valid_to_exclusive,a.temporal_review_status,"
                "a.transition_refs FROM provision p JOIN document_version v USING(version_id) "
                "JOIN source_document d USING(source_id) JOIN applicability a USING(provision_id) "
                "JOIN snapshot_version sv ON sv.version_id=p.version_id "
                "WHERE p.provision_id=%s AND sv.snapshot_id=%s",
                (identifier, snapshot),
            )
            item = cursor.fetchone()
        if item is None:
            return {"provision_id": str(identifier), "available": False}
        version = item["version_id"]
        if version not in versions:
            raw = _raw(root, item["raw_path"], item["raw_sha256"])
            parsed = {
                uuid5(version, p["local_id"]): p for p in extract_provisions(raw.decode("utf-8"))
            }
            attachments = []
            for attachment in item["attachments"]:
                info = {
                    key: attachment.get(key)
                    for key in (
                        "title",
                        "source_url",
                        "collection_status",
                        "extraction_status",
                    )
                }
                if attachment.get("collection_status") == "collected":
                    try:
                        pdf = _raw(root, attachment["raw_path"], attachment["content_hash"])
                        if not pdf.startswith(b"%PDF"):
                            raise ValueError("Not a PDF")
                        name = f"assets/{attachment['content_hash']}.pdf"
                        assets[name] = pdf
                        info.update(local_path=name, content_hash=attachment["content_hash"])
                    except (OSError, ValueError, KeyError):
                        info["local_status"] = "missing_or_invalid"
                attachments.append(info)
            versions[version] = (parsed, attachments)
        original = versions[version][0].get(identifier)
        if original is None or any(
            original[key] != item[key] for key in ("text", "article", "source_offsets")
        ):
            raise ValueError("Stored provision differs from original source")
        expected_parent = original["parent_local_id"]
        if item["parent_id"] != (uuid5(version, expected_parent) if expected_parent else None):
            raise ValueError("Stored parent differs from original source")
        relations = connection.execute(
            "SELECT to_id,relation_type,target_locator,verified FROM provision_relation "
            "WHERE from_id=%s ORDER BY relation_id",
            (identifier,),
        ).fetchall()
        result = {
            key: item[key]
            for key in (
                "provision_id",
                "version_id",
                "title",
                "document_type",
                "article",
                "paragraph",
                "item",
                "text",
                "source_offsets",
                "parent_id",
                "source_url",
                "raw_sha256",
                "promulgated_at",
                "effective_from",
                "collected_at",
                "valid_from",
                "valid_to_exclusive",
                "temporal_review_status",
                "transition_refs",
            )
        }
        result.update(
            available=True,
            attachments=versions[version][1],
            relations=[
                dict(
                    zip(
                        ("to_id", "relation_type", "target_locator", "verified"),
                        relation,
                        strict=True,
                    )
                )
                for relation in relations
            ],
        )
        # JSON conversion also canonicalizes UUID/date values for the offline browser.
        provisions[cache_key] = json.loads(_json(result))
        return provisions[cache_key]

    entries = []
    for row in rows:
        example = Example.model_validate(row["payload"])
        if example.split != "dev" or example.dataset_id not in dataset_ids:
            raise ValueError("Development row and payload disagree")
        if content_hash(example) != row["content_hash"]:
            raise ValueError("Example content hash mismatch")
        evidence = []
        for ref in example.evidence:
            item = provision(example.snapshot_id, ref.provision_id)
            context = []
            seen = {str(ref.provision_id)}
            parent = item.get("parent_id")
            while parent:
                if parent in seen:
                    raise ValueError("Cyclic source hierarchy")
                seen.add(parent)
                context.append(provision(example.snapshot_id, UUID(parent)))
                parent = context[-1].get("parent_id")
            evidence.append(
                {
                    "source": item,
                    "quote": ref.quote,
                    "quote_matches": item["available"] and ref.quote in item["text"],
                    "parents": context,
                }
            )
        entries.append(
            {
                "example": example.model_dump(mode="json"),
                "revision": row["revision"],
                "content_hash": row["content_hash"],
                "review_status": row["review_status"],
                "template": review_template(example, row["revision"]),
                "evidence": evidence,
            }
        )
    body = {"schema_version": 1, "kind": "development-review-workbench", "examples": entries}
    bundle_id = hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
    return {**body, "bundle_id": bundle_id, "created_at": datetime.now(UTC).isoformat()}, assets


def render_workbench(bundle: dict) -> str:
    template = (
        files("finreg").joinpath("templates/review_workbench.html").read_text(encoding="utf-8")
    )
    # Embedded JSON must not close a script element, including malicious source text.
    data = _json(bundle).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return template.replace("__BUNDLE_JSON__", data)


def write_workbench(root: Path, output: Path, bundle: dict, assets: dict[str, bytes]) -> dict:
    output = output.resolve()
    if not output.is_relative_to(root.resolve() / "artifacts" / "review-workbench"):
        raise ValueError("Output must be inside artifacts/review-workbench")
    html = render_workbench(bundle)
    for name in assets:
        if not re.fullmatch(r"assets/[a-f0-9]{64}\.pdf", name):
            raise ValueError("Unexpected asset name")
    output.mkdir(parents=True, exist_ok=False)
    (output / "index.html").write_text(html, encoding="utf-8")
    (output / "bundle.json").write_text(_json(bundle) + "\n", encoding="utf-8")
    for name, raw in assets.items():
        (output / name).parent.mkdir(exist_ok=True)
        (output / name).write_bytes(raw)
    summary = {
        "bundle_id": bundle["bundle_id"],
        "example_count": len(bundle["examples"]),
        "evidence_count": sum(len(entry["evidence"]) for entry in bundle["examples"]),
        "local_pdf_count": len(assets),
        "database_modified": False,
        "files": {
            path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    }
    (output / "manifest.json").write_text(_json(summary) + "\n", encoding="utf-8")
    return summary

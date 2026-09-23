"""Collect pinned official versions and register a candidate (never active) snapshot."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from finreg.config import Settings
from finreg.corpus import collect_source, create_snapshot, ingest_source
from finreg.migrate import connect_database, migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-manifest", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.offline_manifest:
        manifest = json.loads(args.offline_manifest.read_text(encoding="utf-8"))
    else:
        sources = json.loads((root / "configs/sources.json").read_text(encoding="utf-8"))
        manifest = {"created_at": datetime.now(UTC).isoformat(), "sources": []}
        for source in sources:
            print(f"Collecting {source['official_id']}", flush=True)
            manifest["sources"].append(collect_source(root, source))
        directory = root / "data/manifests"
        directory.mkdir(parents=True, exist_ok=True)
        name = datetime.now(UTC).strftime("corpus-%Y%m%dT%H%M%S%fZ.json")
        path = directory / name
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Manifest: {path.relative_to(root)}", flush=True)
    with connect_database(Settings()) as connection:
        migrate(connection, root / "migrations")
        with connection.transaction():
            versions = [ingest_source(connection, root, source) for source in manifest["sources"]]
            snapshot = create_snapshot(connection, versions)
        print(f"Candidate snapshot: {snapshot}; activation withheld pending human review")


if __name__ == "__main__":
    main()

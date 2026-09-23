"""Audit candidate split membership without printing held-out questions or answers."""

import argparse
import json
from pathlib import Path

from finreg.config import Settings
from finreg.evaluation_data import refresh_audit
from finreg.migrate import connect_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/manifests/evaluation-v1.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    with connect_database(Settings()) as connection:
        report = refresh_audit(connection, manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "findings"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()

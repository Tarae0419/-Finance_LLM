"""Check project wiki provenance and links without reading evaluation data."""

import argparse
import json
from pathlib import Path

from finreg.wiki import check_wiki, source_hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--source-hashes", action="store_true", help="print hashes; never write them"
    )
    args = parser.parse_args()
    if args.source_hashes:
        try:
            hashes = source_hashes(args.root)
        except (OSError, ValueError):
            print("Cannot read registered project sources; check the manifest and paths.")
            return 1
        print(json.dumps(hashes, ensure_ascii=True, indent=2))
        return 0
    issues = check_wiki(args.root)
    for issue in issues:
        print(issue)
    print(f"Wiki check: {len(issues)} issue(s). Structural checks only; no semantic review.")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

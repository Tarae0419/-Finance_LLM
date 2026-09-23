"""Download a pinned public safetensors model; verify and record local file hashes."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".cache/huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main():
    from huggingface_hub import HfApi, snapshot_download

    config = json.loads((ROOT / "configs/model_runtime.json").read_text(encoding="utf-8"))
    metadata = HfApi(token=False).model_info(
        config["model_id"], revision=config["revision"], files_metadata=True
    )
    if metadata.sha != config["revision"] or metadata.card_data.license != "apache-2.0":
        raise ValueError("Unexpected model revision or license")
    directory = ROOT / config["local_directory"]
    snapshot_download(
        repo_id=config["model_id"],
        revision=config["revision"],
        local_dir=directory,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "LICENSE", "README.md"],
        max_workers=3,
        token=False,
    )
    files = []
    for item in metadata.siblings:
        path = directory / item.rfilename
        if not path.is_file():
            continue
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if item.lfs and digest != item.lfs.sha256:
            raise ValueError(f"Downloaded weight hash mismatch: {item.rfilename}")
        files.append({"path": item.rfilename, "sha256": digest, "bytes": path.stat().st_size})
    manifest = {
        "model_id": config["model_id"],
        "revision": metadata.sha,
        "tokenizer_revision": config["tokenizer_revision"],
        "license": config["license"],
        "downloaded_at": datetime.now(UTC).isoformat(),
        "files": files,
        "source_url": f"https://huggingface.co/{config['model_id']}/tree/{metadata.sha}",
    }
    path = ROOT / "data/manifests/model-qwen3-4b.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(files)} model files; manifest: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

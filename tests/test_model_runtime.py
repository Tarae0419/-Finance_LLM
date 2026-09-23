import hashlib
import json

import pytest

from finreg.model_runtime import read_runtime_config, verify_model_files


@pytest.fixture
def local_model(tmp_path):
    config = {
        "model_id": "fixture",
        "revision": "a" * 40,
        "tokenizer_revision": "a" * 40,
        "license": "fixture-only",
        "local_directory": "models/fixture",
    }
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/model_runtime.json").write_text(json.dumps(config), encoding="utf-8")
    directory = tmp_path / "models/fixture"
    directory.mkdir(parents=True)
    files = []
    for name in (
        "config.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "LICENSE",
        "model.safetensors",
    ):
        raw = b"synthetic test bytes, never loaded as weights"
        (directory / name).write_bytes(raw)
        files.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {**config, "files": files}
    (tmp_path / "data/manifests").mkdir(parents=True)
    (tmp_path / "data/manifests/model-qwen3-4b.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return tmp_path, config, directory


def test_pinned_files_verify_and_corruption_is_rejected(local_model):
    root, config, directory = local_model
    assert read_runtime_config(root) == config
    assert verify_model_files(root, config) == directory
    weights = directory / "model.safetensors"
    weights.write_bytes(b"x" * weights.stat().st_size)
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_model_files(root, config)


def test_manifest_revision_and_path_must_match(local_model):
    root, config, _ = local_model
    with pytest.raises(ValueError, match="manifest does not match"):
        verify_model_files(root, {**config, "revision": "b" * 40})
    with pytest.raises(ValueError, match="inside the local models"):
        verify_model_files(root, {**config, "local_directory": "../outside"})


def test_mutable_revision_is_not_accepted(local_model):
    root, config, _ = local_model
    (root / "configs/model_runtime.json").write_text(
        json.dumps({**config, "revision": "main"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="immutable"):
        read_runtime_config(root)

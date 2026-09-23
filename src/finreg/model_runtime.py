"""Offline-only model loading helpers, separate from the legal answer API."""

import hashlib
import json
import re
from pathlib import Path


def read_runtime_config(root: Path) -> dict:
    config = json.loads((root / "configs/model_runtime.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-f0-9]{40}", config["revision"]):
        raise ValueError("A full immutable model revision is required")
    if config["tokenizer_revision"] != config["revision"]:
        raise ValueError("Tokenizer and model revisions must match for this probe")
    return config


def verify_model_files(root: Path, config: dict) -> Path:
    directory = (root / config["local_directory"]).resolve()
    if not directory.is_relative_to((root / "models").resolve()):
        raise ValueError("Model files must stay inside the local models directory")
    manifest = json.loads((root / "data/manifests/model-qwen3-4b.json").read_text(encoding="utf-8"))
    for name in ("model_id", "revision", "tokenizer_revision", "license"):
        if manifest[name] != config[name]:
            raise ValueError(f"Model manifest does not match configuration: {name}")
    names = {entry["path"] for entry in manifest["files"]}
    if not {"config.json", "tokenizer_config.json", "tokenizer.json", "LICENSE"} <= names:
        raise ValueError("Incomplete model manifest")
    if not any(name.endswith(".safetensors") for name in names):
        raise ValueError("No safetensors weights in manifest")
    for entry in manifest["files"]:
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Model file path escapes the model directory")
        if path.stat().st_size != entry["bytes"]:
            raise ValueError(f"Model file size mismatch: {entry['path']}")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"Model file hash mismatch: {entry['path']}")
    return directory


def load_local_model(directory: Path, config: dict):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required; this probe does not silently fall back to CPU")
    if config["compute_dtype"] != "bfloat16" or not torch.cuda.is_bf16_supported():
        raise RuntimeError("This runtime configuration requires BF16 support")
    tokenizer = AutoTokenizer.from_pretrained(
        directory, local_files_only=True, trust_remote_code=False
    )
    model = AutoModelForCausalLM.from_pretrained(
        directory,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        device_map={"": 0},
        dtype=torch.bfloat16,
        attn_implementation=config["attention"],
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type=config["quantization"],
            bnb_4bit_use_double_quant=config["double_quantization"],
            bnb_4bit_compute_dtype=torch.bfloat16,
        ),
    )
    return model, tokenizer

"""Measure local inference and a synthetic adapter probe, never domain training or evaluation."""

import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from threading import Event, Thread
from time import perf_counter

from finreg.model_runtime import load_local_model, read_runtime_config, verify_model_files

ROOT = Path(__file__).resolve().parents[1]
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("HF_HOME", str(ROOT / ".cache/huggingface"))


class Measurement:
    def __enter__(self):
        import psutil
        import torch

        self.process = psutil.Process()
        self.rss_peak = self.process.memory_info().rss
        self.done = Event()
        self.thread = Thread(target=self.sample, daemon=True)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        self.started = perf_counter()
        self.thread.start()
        return self

    def sample(self):
        while not self.done.wait(0.02):
            self.rss_peak = max(self.rss_peak, self.process.memory_info().rss)

    def __exit__(self, *_):
        import torch

        try:
            torch.cuda.synchronize()
        finally:
            self.seconds = perf_counter() - self.started
            self.done.set()
            self.thread.join()
            self.result = {
                "seconds": self.seconds,
                "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "gpu_peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                "process_rss_peak_bytes": max(self.rss_peak, self.process.memory_info().rss),
            }


def generate(model, inputs, tokens, tokenizer, *, fixed=False):
    import torch

    with torch.inference_mode():
        return model.generate(
            **inputs,
            max_new_tokens=tokens,
            min_new_tokens=tokens if fixed else 0,
            do_sample=False,
            temperature=1.0,
            top_p=1.0,
            top_k=50,
            pad_token_id=tokenizer.eos_token_id,
        )


def inference_probe(model, tokenizer, config):
    import torch

    model.eval()
    messages = [
        {
            "role": "user",
            "content": "사과 두 개와 사과 한 개를 합하면 몇 개인지 한국어 한 문장으로 답하세요.",
        }
    ]
    chat = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(chat, return_tensors="pt").to("cuda")
    with Measurement() as smoke:
        output = generate(model, inputs, config["output_tokens"], tokenizer)
    answer = tokenizer.decode(output[0, inputs.input_ids.shape[1] :], skip_special_tokens=True)
    results = {
        "korean_smoke": {
            "prompt": messages[0]["content"],
            "output": answer,
            "input_tokens": inputs.input_ids.shape[1],
            **smoke.result,
        },
        "fixed_length": [],
    }
    text = "가상 자원 측정용 문장입니다. 실제 법규나 고객 정보가 아닙니다. " * 1000
    prefix = tokenizer.encode(text, add_special_tokens=False)
    for length in config["input_lengths"]:
        ids = torch.tensor([prefix[:length]], device="cuda")
        inputs = {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
        warm = generate(model, inputs, 8, tokenizer, fixed=True)
        del warm
        samples = []
        for _ in range(config["repetitions"]):
            with Measurement() as measured:
                output = generate(model, inputs, config["output_tokens"], tokenizer, fixed=True)
            generated = output.shape[1] - length
            samples.append(
                {
                    **measured.result,
                    "output_tokens": generated,
                    "output_tokens_per_second": generated / measured.seconds,
                }
            )
            del output
        results["fixed_length"].append(
            {
                "input_tokens": length,
                "input_ids_sha256": hashlib.sha256(
                    json.dumps(prefix[:length]).encode()
                ).hexdigest(),
                "warmup_output_tokens": 8,
                "samples": samples,
                "median_seconds": median(item["seconds"] for item in samples),
            }
        )
        print(f"Inference complete: {length} input tokens", flush=True)
    return results


def adapter_probe(model, tokenizer, config, directory):
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    gc.collect()
    torch.cuda.empty_cache()
    model.config.use_cache = False
    with Measurement() as measured:
        model = prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
        )
        model = get_peft_model(
            model,
            LoraConfig(
                r=config["lora_rank"],
                lora_alpha=config["lora_alpha"],
                target_modules=["q_proj", "v_proj"],
                lora_dropout=0.0,
                task_type="CAUSAL_LM",
            ),
        )
        model.train()
        trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
        parameter_count = sum(parameter.numel() for parameter in trainable)
        optimizer = torch.optim.AdamW(trainable, lr=config["learning_rate"])
        # Purely synthetic hardware workload, not a reviewed legal example or SFT result.
        generator = torch.Generator(device="cuda").manual_seed(config["seed"])
        ids = torch.randint(
            100, 10000, (1, config["training_sequence_length"]), generator=generator, device="cuda"
        )
        losses = []
        for step in range(config["training_steps"]):
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                output = model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=ids)
            loss = output.loss
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite synthetic probe loss")
            loss.backward()
            norm = torch.nn.utils.clip_grad_norm_(trainable, 1.0, error_if_nonfinite=True)
            if norm.item() == 0:
                raise RuntimeError("No adapter gradient")
            optimizer.step()
            losses.append(float(loss.detach()))
            del output, loss
            print(f"Synthetic adapter step {step + 1} complete", flush=True)
        optimizer.zero_grad(set_to_none=True)
    adapter_dir = directory / "synthetic-adapter"
    model.save_pretrained(adapter_dir, safe_serialization=True)
    trained = {
        name: hashlib.sha256(parameter.detach().float().cpu().numpy().tobytes()).hexdigest()
        for name, parameter in model.named_parameters()
        if parameter.requires_grad
    }
    marker = {
        "synthetic_only": True,
        "eligible_for_release": False,
        "base_revision": config["revision"],
        "data": "seeded random token IDs",
    }
    (adapter_dir / "PROBE_ONLY.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return {
        **measured.result,
        "steps": config["training_steps"],
        "batch_size": 1,
        "sequence_length": config["training_sequence_length"],
        "trainable_parameters": parameter_count,
        "target_modules": ["q_proj", "v_proj"],
        "losses": losses,
        "optimizer": "torch.optim.AdamW",
        "gradient_checkpointing": True,
        "checkpoint_use_reentrant": False,
        "adapter_parameter_hashes": trained,
        "adapter_path": adapter_dir.relative_to(ROOT).as_posix(),
        "synthetic_only": True,
        "eligible_for_release": False,
    }


def reload_probe(model_path, config, training):
    from peft import PeftModel

    with Measurement() as measured:
        base, tokenizer = load_local_model(model_path, config)
        restored = PeftModel.from_pretrained(
            base, ROOT / training["adapter_path"], local_files_only=True
        )
        reloaded = dict(restored.named_parameters())
        matched = all(
            value
            == hashlib.sha256(reloaded[name].detach().float().cpu().numpy().tobytes()).hexdigest()
            for name, value in training["adapter_parameter_hashes"].items()
        )
        if not matched:
            raise RuntimeError("Reloaded adapter parameters differ")
        restored.eval()
        inputs = tokenizer("가상 실행 확인", return_tensors="pt").to("cuda")
        generated = generate(restored, inputs, 8, tokenizer, fixed=True)
    return {
        **measured.result,
        "fresh_base_model": True,
        "adapter_parameters_equal": matched,
        "output_tokens": generated.shape[1] - inputs.input_ids.shape[1],
    }


def main():
    directory = ROOT / "artifacts/probes" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "purpose": "S1-04 hardware feasibility; no legal quality evaluation",
        "paid_resources": False,
    }
    path = directory / "report.json"

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        import psutil
        import torch
        from transformers import set_seed

        config = read_runtime_config(ROOT)
        report["config"] = config
        report["generation"] = {
            "do_sample": False,
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 50,
            "use_cache": True,
        }
        report["source_hashes"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("scripts/probe_model.py", "src/finreg/model_runtime.py", "uv.lock")
        }
        print("Verifying pinned local files", flush=True)
        model_path = verify_model_files(ROOT, config)
        set_seed(config["seed"])
        report["environment"] = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in (
                    "torch",
                    "transformers",
                    "bitsandbytes",
                    "peft",
                    "accelerate",
                    "psutil",
                )
            },
            "ram_bytes": psutil.virtual_memory().total,
            "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0),
            "gpu_total_bytes": torch.cuda.get_device_properties(0).total_memory,
            "gpu_free_bytes_before_load": torch.cuda.mem_get_info()[0],
            "nvidia_smi": subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version,memory.total,memory.used",
                    "--format=csv,noheader",
                ],
                text=True,
            ).strip(),
        }
        print("Loading 4-bit model on CUDA", flush=True)
        with Measurement() as load:
            model, tokenizer = load_local_model(model_path, config)
        report["loading"] = load.result
        report["inference"] = inference_probe(model, tokenizer, config)
        save()
        report["adapter_probe"] = adapter_probe(model, tokenizer, config, directory)
        del model
        gc.collect()
        torch.cuda.empty_cache()
        print("Reloading adapter onto a fresh base model", flush=True)
        report["adapter_reload"] = reload_probe(model_path, config, report["adapter_probe"])
        report["status"] = "completed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        save()
        print(f"Report: {path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()

"""Train upstream WeKWS MDTC-small using synthetic speakers only.

Architecture and max-pooling loss are imported, not reimplemented. Human and
previous report audio never enter training, checkpoint or threshold selection.
Exports a causal stateful ONNX model; verifies full/chunked/Torch parity.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import torch

from scripts.benchmark_wake import load_wav
from scripts.light_wake_features import features


def model_config():
    return {
        "input_dim": 80,
        "output_dim": 1,
        "hidden_dim": 32,
        "preprocessing": {"type": "linear"},
        "backbone": {
            "type": "mdtc",
            "num_stack": 3,
            "stack_size": 4,
            "kernel_size": 5,
            "hidden_dim": 32,
            "causal": True,
        },
    }


def select_threshold(positive, negative):
    # Use the validation negatives only. The grid is fixed before human evaluation.
    choices = []
    for threshold in np.arange(0.5, 1.0, 0.005):
        recall = float(np.mean(positive >= threshold))
        false = float(np.mean(negative >= threshold))
        choices.append((recall - 4 * false, -false, recall, -threshold, float(threshold)))
    return max(choices)[-1]


def train(args):
    torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    upstream = subprocess.check_output(
        ["git", "-C", str(args.toolkit), "rev-parse", "HEAD"], text=True
    ).strip()
    sys.path.insert(0, str(args.toolkit.resolve()))
    from wekws.model.kws_model import init_model
    from wekws.model.loss import max_pooling_loss
    from wekws.model.cmvn import GlobalCMVN

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    splits = {
        s: {r["speaker_id"] for r in manifest["records"] if r["split"] == s}
        for s in ("train", "validation", "test")
    }
    if any(
        splits[a] & splits[b] for a, b in (("train", "validation"), ("train", "test"), ("validation", "test"))
    ):
        raise ValueError("Speaker leakage")
    if any(r.get("natural_human_recording") for r in manifest["records"]):
        raise ValueError("Human audio is reserved for final evaluation")
    args.output.mkdir(parents=True, exist_ok=True)
    cache = args.output / "features.npz"
    original = [r for r in manifest["records"] if r["split"] != "test"]
    rows = [r for r in original for _ in range(3 if r["split"] == "train" else 1)]
    if cache.exists():
        data = np.load(cache)
        bank = [data[str(i)] for i in range(len(rows))]
    else:
        bank = []
        for r in original:
            if hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() != r["sha256"]:
                raise ValueError("Changed source audio")
            samples = load_wav(r["path"])
            bank.append(features(samples))
            if r["split"] == "train":
                for _ in range(2):
                    audio = samples.copy()
                    for delay_ms, decay in [(45, 0.25), (95, 0.15), (155, 0.08)]:
                        delay = delay_ms * 16
                        audio[delay:] += samples[:-delay] * decay * rng.uniform(0, 1)
                    active = samples[np.abs(samples) > max(0.001, np.max(np.abs(samples)) * 0.03)]
                    rms = float(np.sqrt(np.mean(active**2))) if len(active) else 0.01
                    audio += rng.normal(0, rms / 10 ** (rng.uniform(15, 35) / 20), len(audio)).astype(
                        np.float32
                    )
                    audio *= rng.uniform(0.15, 1.4)
                    bank.append(features(np.clip(audio, -1, 1)))
        np.savez_compressed(cache, **{str(i): x for i, x in enumerate(bank)})
    train_idx = np.array([i for i, r in enumerate(rows) if r["split"] == "train"])
    val_idx = [i for i, r in enumerate(rows) if r["split"] == "validation"]
    all_train = np.concatenate([bank[i] for i in train_idx])
    mean, std = all_train.mean(0), np.maximum(all_train.std(0), 1)
    model = init_model(model_config())
    model.global_cmvn = GlobalCMVN(torch.tensor(mean), torch.tensor(1 / std))
    parameter_count = sum(p.numel() for p in model.parameters())
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=0.00005)
    log, best = [], None
    started = time.perf_counter()

    def batch(indices):
        xs = []
        for i in indices:
            x = bank[int(i)].copy()
            xs.append(x)
        lengths = torch.tensor([len(x) for x in xs])
        padded = np.broadcast_to(mean, (len(xs), max(lengths).item(), 80)).copy()
        for j, x in enumerate(xs):
            padded[j, : len(x)] = x
        labels = torch.tensor([0 if rows[int(i)]["positive"] else -1 for i in indices])
        return torch.tensor(padded), lengths, labels

    # Exact masks use an explicit width; no dependency on model outputs or evaluation audio.
    def get_batch(indices, augment=False):
        if not augment:
            return batch(indices)
        xs = []
        for i in indices:
            x = bank[int(i)].copy() + rng.uniform(-3, 1)
            x += rng.normal(0, rng.uniform(0, 0.6), x.shape).astype(np.float32)
            if rng.random() < 0.6:
                start = int(rng.integers(0, max(1, len(x) - 20)))
                x[start : start + int(rng.integers(5, 20))] = mean
            if rng.random() < 0.6:
                start, width = int(rng.integers(0, 68)), int(rng.integers(3, 12))
                x[:, start : start + width] = mean[start : start + width]
            xs.append(x.astype(np.float32))
        lengths = torch.tensor([len(x) for x in xs])
        padded = np.broadcast_to(mean, (len(xs), int(lengths.max()), 80)).copy()
        for j, x in enumerate(xs):
            padded[j, : len(x)] = x
        labels = torch.tensor([0 if rows[int(i)]["positive"] else -1 for i in indices])
        return torch.tensor(padded), lengths, labels

    for epoch in range(1, args.epochs + 1):
        model.train()
        losses = []
        order = rng.permutation(train_idx)
        for start in range(0, len(order), args.batch_size):
            x, lengths, labels = get_batch(order[start : start + args.batch_size], True)
            optimizer.zero_grad()
            output, _ = model(x)
            loss, _ = max_pooling_loss(output, labels, lengths, min_duration=20)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            losses.append(loss.item())
        model.eval()
        scores, validation_loss = [], []
        with torch.inference_mode():
            for start in range(0, len(val_idx), args.batch_size):
                idx = val_idx[start : start + args.batch_size]
                x, lengths, labels = get_batch(idx)
                output, _ = model(x)
                loss, _ = max_pooling_loss(output, labels, lengths, min_duration=20)
                validation_loss.append(loss.item())
                scores.extend(float(output[j, : int(lengths[j]), 0].max()) for j in range(len(idx)))
        pos = np.array([s for s, i in zip(scores, val_idx) if rows[i]["positive"]])
        neg = np.array([s for s, i in zip(scores, val_idx) if not rows[i]["positive"]])
        threshold = select_threshold(pos, neg)
        recall, false = float(np.mean(pos >= threshold)), float(np.mean(neg >= threshold))
        result = {
            "epoch": epoch,
            "train_loss": float(np.mean(losses)),
            "validation_loss": float(np.mean(validation_loss)),
            "threshold": threshold,
            "validation_recall": recall,
            "validation_false_rate": false,
            "elapsed_s": time.perf_counter() - started,
        }
        log.append(result)
        rank = (recall - 4 * false, -result["validation_loss"])
        if best is None or rank > best:
            best = rank
            torch.save(
                {
                    "weights": model.state_dict(),
                    "mean": mean,
                    "std": std,
                    "selection": result,
                    "config": model_config(),
                    "parameter_count": parameter_count,
                },
                args.output / "selected.pt",
            )
        (args.output / "training.json").write_text(
            json.dumps(
                {
                    "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "upstream_commit": upstream,
                    "seed": args.seed,
                    "threads": args.threads,
                    "parameter_count": parameter_count,
                    "human_training_recordings": 0,
                    "test_used_for_selection": False,
                    "history": log,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(json.dumps(result), flush=True)
    checkpoint = torch.load(args.output / "selected.pt", weights_only=False)
    model.load_state_dict(checkpoint["weights"])
    model.eval()
    cache_tensor = torch.zeros(1, 32, model.backbone.padding)
    dummy = torch.randn(1, 20, 80)
    torch.onnx.export(
        model,
        (dummy, cache_tensor),
        args.output / "model.onnx",
        input_names=["input", "cache"],
        output_names=["output", "r_cache"],
        dynamic_axes={"input": {1: "time"}, "output": {1: "time"}},
        opset_version=17,
        dynamo=False,
    )
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        str(args.output / "model.onnx"), options, providers=["CPUExecutionProvider"]
    )
    x = bank[val_idx[0]][None]
    zero = cache_tensor.numpy()
    with torch.inference_mode():
        reference = model(torch.tensor(x), cache_tensor)[0].numpy()
    whole = session.run(None, {"input": x, "cache": zero})[0]
    cache_state, chunks = zero, []
    for offset in range(0, x.shape[1], 4):
        out, cache_state = session.run(None, {"input": x[:, offset : offset + 4], "cache": cache_state})
        chunks.append(out)
    streamed = np.concatenate(chunks, axis=1)
    torch_error, stream_error = (
        float(np.max(np.abs(reference - whole))),
        float(np.max(np.abs(streamed - whole))),
    )
    if torch_error > 2e-5 or stream_error > 2e-5:
        raise ValueError(f"ONNX parity failure {torch_error} {stream_error}")
    metadata = {
        "kind": "WeKWS MDTC-small binary causal wake detector",
        "parameter_count": parameter_count,
        "cache_shape": list(zero.shape),
        "threshold": checkpoint["selection"]["threshold"],
        "selection": checkpoint["selection"],
        "torch_onnx_max_error": torch_error,
        "chunk_full_max_error": stream_error,
        "upstream_commit": upstream,
        "model_sha256": hashlib.sha256((args.output / "model.onnx").read_bytes()).hexdigest(),
        "human_training_recordings": 0,
        "runtime_imports_torch": False,
    }
    (args.output / "model-metadata.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("state/light-kws-20261013/corpus"))
    parser.add_argument("--toolkit", type=Path, default=Path("models/wekws-toolkit"))
    parser.add_argument("--output", type=Path, default=Path("models/light-kws-20261013/mdtc-small"))
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261013)
    train(parser.parse_args())


if __name__ == "__main__":
    main()

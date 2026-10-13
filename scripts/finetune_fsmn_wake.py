"""Synthetic-only fine-tuning of upstream pretrained FSMN character-phone CTC.

Keeps all human recordings out of training and checkpoint selection. The
checkpoint is selected by CTC loss on distinct synthetic validation speakers.
No transcription engine or conversational fallback is added to the application.
"""

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import torch

from scripts.light_wake_features import features, load_wav


def lfr_cmvn(fbank, cmvn):
    # Same 5-stack / 3-skip as the upstream frontend, including replicated edges.
    centers = np.arange(0, len(fbank), 3)
    offsets = np.arange(-2, 3)
    indexes = np.clip(centers[:, None] + offsets[None], 0, len(fbank) - 1)
    return ((fbank[indexes].reshape(len(centers), 400) + cmvn[0]) * cmvn[1]).astype(np.float32)


def focused_targets(labels, mapping, background_id):
    result = []
    for label in labels:
        token = mapping.get(int(label), background_id)
        # Collapse background spans, but keep repeated wake syllables distinct.
        if token != background_id or not result or result[-1] != background_id:
            result.append(token)
    return np.asarray(result)


def focused_log_probs(log_probs, keyword_ids, other_ids):
    return torch.cat(
        [
            log_probs[:, :, [0, *keyword_ids]],
            log_probs[:, :, other_ids].logsumexp(2, keepdim=True),
        ],
        dim=2,
    )


def train(args):
    from funasr import AutoModel
    from funasr.utils.kws_utils import query_token_set

    torch.set_num_threads(2)
    torch.manual_seed(20261013)
    rng = np.random.default_rng(20261013)
    root = args.pretrained.resolve()
    model = AutoModel(
        model=str(root),
        init_param=str(root / "funasr/basetrain_fsmn_4e_l10r2_250_128_fdim80_t2599.pt"),
        device="cpu",
        ncpu=2,
        disable_update=True,
        disable_pbar=True,
        disable_log=True,
        frontend_conf={"dither": 0.0},
    )
    cmvn = model.kwargs["frontend"].cmvn.numpy()
    vocabulary = dict(zip(model.kwargs["token_list"], range(len(model.kwargs["token_list"]))))
    lexicon = model.kwargs["tokenizer"].seg_dict
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    records = [r for r in manifest["records"] if r["split"] in ("train", "validation")]
    rows = []
    skipped = []
    for r in records:
        if r.get("natural_human_recording"):
            raise ValueError("Human recordings are evaluation only")
        text = "".join(c for c in r["text"] if "\u4e00" <= c <= "\u9fff")
        tokens, ids = query_token_set(text, vocabulary, lexicon)
        if not ids or any(model.kwargs["token_list"][i] in ("<unk>", "<blank>") for i in ids):
            skipped.append({"id": r["id"], "tokens": tokens})
            continue
        rows.append((r, lfr_cmvn(features(load_wav(r["path"])), cmvn), np.array(ids)))
    _, keyword_ids = query_token_set("嘟你好", vocabulary, lexicon)
    keyword_ids = list(dict.fromkeys(keyword_ids))
    background_id = len(keyword_ids) + 1
    mapping = {token: i + 1 for i, token in enumerate(keyword_ids)}
    other_ids = [i for i in range(len(vocabulary)) if i not in [0, *keyword_ids]]
    if args.objective == "keyword-ctc":
        rows = [(r, x, focused_targets(y, mapping, background_id)) for r, x, y in rows]
    train_rows = [r for r in rows if r[0]["split"] == "train"]
    val_rows = [r for r in rows if r[0]["split"] == "validation"]
    args.output.mkdir(parents=True, exist_ok=True)
    network = model.model
    if args.init_checkpoint:
        network.load_state_dict(torch.load(args.init_checkpoint, weights_only=True))
    network.train()
    optimizer = torch.optim.Adam(network.parameters(), lr=args.learning_rate, weight_decay=0.00001)
    criterion = torch.nn.CTCLoss(blank=0, reduction="mean", zero_infinity=True)
    history, best = [], float("inf")
    started = time.perf_counter()

    def batch(rows, augment):
        xs, labels = [], []
        for _, x, y in rows:
            x = x.copy()
            if augment:
                x += rng.normal(0, 0.05, x.shape).astype(np.float32)
                if rng.random() < 0.5:
                    offset = int(rng.integers(0, max(1, len(x) - 5)))
                    x[offset : offset + 4] = 0
            xs.append(torch.tensor(x))
            labels.append(torch.tensor(y))
        xlen, ylen = torch.tensor([len(x) for x in xs]), torch.tensor([len(y) for y in labels])
        return torch.nn.utils.rnn.pad_sequence(xs, batch_first=True), xlen, torch.cat(labels), ylen

    def loss_of(rows, augment=False):
        x, lengths, target, target_lengths = batch(rows, augment)
        logits = network.encoder(x)
        probabilities = logits.log_softmax(2)
        if args.objective == "keyword-ctc":
            probabilities = focused_log_probs(probabilities, keyword_ids, other_ids)
        return criterion(probabilities.transpose(0, 1), target, lengths, target_lengths)

    stale_epochs = 0
    for epoch in range(1, args.epochs + 1):
        network.train()
        losses = []
        order = rng.permutation(len(train_rows))
        for start in range(0, len(order), 16):
            batch_rows = [train_rows[int(i)] for i in order[start : start + 16]]
            optimizer.zero_grad()
            loss = loss_of(batch_rows, True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(network.parameters(), 5)
            optimizer.step()
            losses.append(float(loss.detach()))
        network.eval()
        with torch.inference_mode():
            validation = [float(loss_of(val_rows[i : i + 16])) for i in range(0, len(val_rows), 16)]
        result = {
            "epoch": epoch,
            "train_loss": float(np.mean(losses)),
            "validation_loss": float(np.mean(validation)),
            "elapsed_s": time.perf_counter() - started,
        }
        history.append(result)
        if result["validation_loss"] < best:
            stale_epochs = 0
            best = result["validation_loss"]
            torch.save(network.state_dict(), args.output / "selected.pt")
            (args.output / "selection.json").write_text(json.dumps(result, indent=2))
        else:
            stale_epochs += 1
        (args.output / "training.json").write_text(
            json.dumps(
                {
                    "history": history,
                    "train_clips": len(train_rows),
                    "validation_clips": len(val_rows),
                    "skipped_unsupported": skipped,
                    "human_training_recordings": 0,
                    "learning_rate": args.learning_rate,
                    "objective": args.objective,
                    "early_stopping_patience": args.patience,
                    "initial_checkpoint_sha256": hashlib.sha256(args.init_checkpoint.read_bytes()).hexdigest()
                    if args.init_checkpoint
                    else None,
                    "initial_checkpoint": str(args.init_checkpoint)
                    if args.init_checkpoint
                    else "official-base",
                    "corpus_sha256": hashlib.sha256((args.corpus / "manifest.json").read_bytes()).hexdigest(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(json.dumps(result), flush=True)
        if args.patience and stale_epochs >= args.patience:
            print(f"Early stop: {stale_epochs} epochs without synthetic validation improvement", flush=True)
            break
    network.load_state_dict(torch.load(args.output / "selected.pt", weights_only=True))
    network.eval()
    torch.onnx.export(
        network.encoder,
        torch.randn(1, 30, 400),
        args.output / "model.onnx",
        input_names=["input"],
        output_names=["logits"],
        dynamic_axes={"input": {1: "time"}, "logits": {1: "time"}},
        opset_version=17,
        dynamo=False,
    )
    np.save(args.output / "cmvn.npy", cmvn)
    tokens, ids = query_token_set("嘟嘟", vocabulary, lexicon)
    from scripts.light_wake_ctc import make_decoder, score, Tensor
    from scripts.train_light_wake import select_threshold

    decoder, decoder_hash = make_decoder(root)
    positive, negative, val_scores = [], [], []
    with torch.inference_mode():
        for record, x, _ in val_rows:
            probabilities = network.encoder(torch.tensor(x)[None]).softmax(2)[0]
            native_score, word = score(decoder, probabilities.numpy())
            # Compare adaptation against the unchanged upstream Torch decoder.
            if decoder._decode_inside(probabilities, None) != decoder._decode_inside(
                Tensor(probabilities.numpy()), None
            ):
                raise ValueError("Upstream decoder adapter parity failure")
            (positive if record["positive"] else negative).append(native_score)
            val_scores.append(
                {"id": record["id"], "positive": record["positive"], "score": native_score, "word": word}
            )
    threshold = select_threshold(np.array(positive), np.array(negative))
    (args.output / "validation-scores.json").write_text(json.dumps(val_scores, ensure_ascii=False, indent=2))
    metadata = {
        "kind": "Pretrained FSMN CTC fine-tuned on synthetic wake/negative speech",
        "parameter_count": sum(p.numel() for p in network.parameters()),
        "training_objective": args.objective,
        "focused_token_ids": keyword_ids if args.objective == "keyword-ctc" else None,
        "wake_tokens": tokens,
        "wake_ids": ids,
        "selection": json.loads((args.output / "selection.json").read_text()),
        "human_training_recordings": 0,
        "model_sha256": hashlib.sha256((args.output / "model.onnx").read_bytes()).hexdigest(),
        "threshold": threshold,
        "decoder_source_sha256": decoder_hash,
        "lookahead_ms": 260,
    }
    (args.output / "model-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(json.dumps(metadata, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("state/light-kws-20261013/corpus"))
    parser.add_argument("--pretrained", type=Path, default=Path("models/fsmn-kws-xiaoyun"))
    parser.add_argument("--output", type=Path, default=Path("models/light-kws-20261013/fsmn-custom"))
    parser.add_argument("--objective", choices=["full-text-ctc", "keyword-ctc"], default="full-text-ctc")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--patience", type=int, default=0)
    parser.add_argument("--learning-rate", type=float, default=0.0002)
    parser.add_argument("--init-checkpoint", type=Path)
    train(parser.parse_args())


if __name__ == "__main__":
    main()

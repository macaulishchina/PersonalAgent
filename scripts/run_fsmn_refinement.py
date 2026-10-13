"""Offline synthetic-only refinement, selection on synthetic validation only.

Train/validation finish and selection is frozen before human evaluation begins.
The installed assistant and all device output remain untouched.
"""

import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

FOLDER = Path("state/fsmn-v2-20261013")
MODELS = Path("models/fsmn-v2-20261013")
CORPUS = FOLDER / "corpus/manifest.json"
RESUME = False


def run(module, arguments, log):
    arguments = list(map(str, arguments))
    if module == "scripts.finetune_fsmn_wake":
        arguments += ["--patience", "5"]
    if module == "scripts.calibrate_light_wake":
        arguments += ["--objective", "balanced"]
    target = Path(arguments[arguments.index("--output") + 1]) if "--output" in arguments else None
    if module == "scripts.finetune_fsmn_wake" and target:
        target = target / "model-metadata.json"
    if module == "scripts.prepare_fsmn_refinement":
        target = CORPUS
    if RESUME and target and target.exists():
        result = json.loads(target.read_text())
        if module != "scripts.calibrate_light_wake" or result.get("selection_policy") == "balanced":
            if module != "scripts.benchmark_light_wake" or result.get("complete"):
                print(json.dumps({"stage": log, "reused_completed": str(target)}), flush=True)
                return
    command = [sys.executable, "-m", module, *arguments]
    print(json.dumps({"stage": log, "command": command}), flush=True)
    with (FOLDER / f"{log}.log").open("w") as out:
        subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, check=True)


def main():
    deadline = time.monotonic() + 3600
    while not (FOLDER / "extra/manifest.json").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Synthetic source did not finish")
        time.sleep(10)
    run("scripts.prepare_fsmn_refinement", [], "prepare")
    run(
        "scripts.calibrate_light_wake",
        [
            "--model",
            "models/light-kws-20261013/fsmn-custom",
            "--corpus",
            CORPUS,
            "--chunk-ms",
            80,
            "--output",
            FOLDER / "calibration-v1.json",
        ],
        "calibrate-v1",
    )
    policies = [("continue", 18, 0.00005), ("rebase", 20, 0.0002), ("focused", 18, 0.00005)]
    plan = {
        "recipes": policies,
        "human_used_for_selection": False,
        "selection_policy": "minimum balanced validation error (mean FNR/FPR), then false clips, then planned recipe order (CTC losses differ by objective)",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (FOLDER / "plan.json").write_text(json.dumps(plan, indent=2))
    for name, epochs, lr in policies:
        extra = (
            ["--init-checkpoint", "models/light-kws-20261013/fsmn-custom/selected.pt"]
            if name in ("continue", "focused")
            else []
        )
        if name == "focused":
            extra += ["--objective", "keyword-ctc"]
        run(
            "scripts.finetune_fsmn_wake",
            [
                "--corpus",
                CORPUS.parent,
                "--output",
                MODELS / name,
                "--epochs",
                epochs,
                "--learning-rate",
                lr,
                *extra,
            ],
            f"train-{name}",
        )
        run(
            "scripts.calibrate_light_wake",
            [
                "--model",
                MODELS / name,
                "--corpus",
                CORPUS,
                "--chunk-ms",
                80,
                "--output",
                FOLDER / f"calibration-{name}.json",
            ],
            f"calibrate-{name}",
        )
        run(
            "scripts.verify_light_wake",
            ["--model", MODELS / name, "--corpus", CORPUS, "--output", FOLDER / f"parity-{name}.json"],
            f"parity-{name}",
        )
    candidates = []
    for name, _, _ in policies:
        calibration = json.loads((FOLDER / f"calibration-{name}.json").read_text())["selection"]
        checkpoint = json.loads((MODELS / name / "selection.json").read_text())
        candidates.append({"name": name, **calibration, "validation_loss": checkpoint["validation_loss"]})
    selected = min(
        candidates,
        key=lambda c: (
            (1 - c["hits"] / c["targets"]) + c["false_clips"] / c["negative_clips"],
            c["false_clips"],
            next(i for i, recipe in enumerate(policies) if recipe[0] == c["name"]),
        ),
    )
    (FOLDER / "frozen-selection.json").write_text(
        json.dumps(
            {
                "selected": selected,
                "candidates": candidates,
                "human_used_for_selection": False,
                "frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            indent=2,
        )
    )
    print(json.dumps({"frozen_selection": selected}), flush=True)
    # Only after the choice above do we open the human evaluation corpus.
    run(
        "scripts.benchmark_light_wake",
        [
            "--model",
            MODELS / selected["name"],
            "--threshold",
            selected["threshold"],
            "--chunk-ms",
            80,
            "--synthetic",
            CORPUS,
            "--standby-seconds",
            60,
            "--output",
            FOLDER / "selected-c80.json",
        ],
        "evaluate-selected",
    )
    run(
        "scripts.benchmark_light_wake",
        [
            "--model",
            "models/light-kws-20261013/fsmn-custom",
            "--threshold",
            0.265,
            "--chunk-ms",
            80,
            "--synthetic",
            CORPUS,
            "--standby-seconds",
            60,
            "--output",
            FOLDER / "v1-c80.json",
        ],
        "evaluate-v1",
    )
    run(
        "scripts.profile_light_wake",
        [
            "--model",
            MODELS / selected["name"],
            "--threshold",
            selected["threshold"],
            "--chunk-ms",
            80,
            "--seconds",
            60,
            "--output",
            FOLDER / "selected-resources.json",
        ],
        "resources-selected",
    )
    print("COMPLETE: training, selection, matched evaluation and resource replication", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    RESUME = parser.parse_args().resume
    main()

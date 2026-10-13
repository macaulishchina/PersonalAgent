"""Install the current FSMN bundle and the upstream Silero voice activity model."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / "models"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wake-bundle", type=Path, help="Directory containing the FSMN V2 manifest/assets")
    parser.add_argument("--model-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    destination = args.model_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    bundle = (args.wake_bundle or destination / "fsmn-v2").expanduser().resolve()
    if not (bundle / "manifest.json").exists():
        parser.error("Provide the trained FSMN V2 bundle via --wake-bundle; no legacy model is downloaded")
    from assistant_runtime.plugins.keyword.fsmn import Fsmn

    Fsmn(bundle, ["嘟嘟", "你好嘟嘟"])
    target = destination / "fsmn-v2"
    if target != bundle:
        target.mkdir(exist_ok=True)
        for name in ("manifest.json", "model.onnx", "cmvn.npy", "tokens.txt", "lexicon.json"):
            shutil.copyfile(bundle / name, target / name)
    vad = destination / "silero_vad.onnx"
    if not vad.exists():
        url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
        partial = vad.with_suffix(".partial")
        urllib.request.urlretrieve(url, partial)
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        partial.replace(vad)
        vad.with_suffix(".metadata.json").write_text(json.dumps({"url": url, "sha256": digest}, indent=2))
    print(f"FSMN V2 and VAD ready: {destination}")


if __name__ == "__main__":
    main()

import json
import torch
from pathlib import Path
from funasr import AutoModel
from scripts.benchmark_wake import load_wav
from scripts.benchmark_asr_candidates import Measure

p = Path("models/fsmn-kws-xiaoyun").resolve()
out = Path("state/light-kws-20261013")
out.mkdir(exist_ok=True)
torch.set_num_threads(1)
rs = json.loads(Path("state/wake-human-comparison-20261013/manifest.json").read_text())["records"][:18]
syn = json.loads(Path("state/wake-comparison/manifest.json").read_text())["records"]
rs += [r for r in syn if r["group"] in ("digital_positive", "digital_negative")]
for variant in ("finetune", "base"):
    kw = (
        {}
        if variant == "finetune"
        else {"init_param": str(p / "funasr/basetrain_fsmn_4e_l10r2_250_128_fdim80_t2599.pt")}
    )
    with Measure() as load:
        m = AutoModel(
            model=str(p),
            device="cpu",
            ncpu=1,
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
            keywords="嘟嘟,你好嘟嘟",
            frontend_conf={"dither": 0.0},
            **kw,
        )
    results = []
    for r in rs:
        with Measure() as metric:
            value = m.generate(input=load_wav(r["path"]), keywords="嘟嘟,你好嘟嘟")[0]["text"]
        results.append(
            {
                "id": r["id"],
                "group": r["group"],
                "text": r.get("text"),
                "positive": bool(r["expected"]),
                "output": value,
                "detected": value.startswith("detected"),
                "measurement": metric.result,
            }
        )
    payload = {
        "variant": variant,
        "parameter_count": sum(t.numel() for t in m.model.parameters()),
        "load": load.result,
        "records": results,
        "wrapper": "FunASR PyTorch reference; utterance inference, no live latency claim",
    }
    (out / f"fsmn-{variant}-reference.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    human = results[:18]
    print(
        variant,
        "human",
        sum(r["detected"] for r in human if r["positive"]),
        "/",
        14,
        "false",
        sum(r["detected"] for r in human if not r["positive"]),
        flush=True,
    )
    print(
        "synthetic",
        sum(r["detected"] for r in results[18:] if r["positive"]),
        sum(r["detected"] for r in results[18:] if not r["positive"]),
        flush=True,
    )

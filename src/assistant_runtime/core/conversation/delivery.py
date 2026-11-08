"""Content ranges with explicit presentation evidence, independent of providers.

Ranges refer to Python Unicode character offsets in the displayed message.
Interleaved blocks stay separate; missing metadata never becomes permission to
speak tool progress or reasoning. Metadata does not change the actual text.
"""


def evidence(value):
    if (
        not isinstance(value, dict)
        or not (
            value.get("phase") in {"final", "commentary"}
            and value.get("evidence") == "provider"
            or value.get("phase") == "unknown"
            and value.get("evidence") == "conflict"
        )
        or type(value.get("block")) is not int
        or not 0 <= value["block"] < 128
    ):
        return None
    return {k: value[k] for k in ("block", "phase", "evidence")}


def append(presentation, offset, length, block, delivery):
    result = dict(presentation or {})
    ranges = [dict(r) for r in result.get("text_ranges", [])]
    if type(block) is not int or not 0 <= block < 128 or result.get("range_limit"):
        result["range_limit"] = True
        return result
    known = evidence(delivery)
    phase, source = (known["phase"], known["evidence"]) if known else ("unknown", "pending")
    current = {"start": offset, "end": offset + length, "block": block, "phase": phase, "evidence": source}
    if (
        ranges
        and all(ranges[-1].get(k) == current[k] for k in ("block", "phase", "evidence"))
        and ranges[-1]["end"] == offset
    ):
        ranges[-1]["end"] += length
    elif len(ranges) < 128:
        ranges.append(current)
    else:
        result["range_limit"] = True
    result["text_ranges"] = ranges
    return result


def classify(presentation, delivery):
    result = dict(presentation or {})
    known = evidence(delivery)
    if known:
        result["text_ranges"] = [
            {**r, "phase": known["phase"], "evidence": known["evidence"]}
            if r["block"] == known["block"]
            else dict(r)
            for r in result.get("text_ranges", [])
        ]
    return result

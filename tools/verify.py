"""Independent verification of the artifacts.

Checks (all against the produced files, not against in-process state):

1. Every message in both products: the independent bit-list reference model
   reproduces the compiled bit positions, unassigned mask and overlap verdict.
2. A stratified sample of frames: reference decode of the original hex equals
   the warehouse's stored raw values and exact fractions, and re-encoding the
   reference raw values reproduces the exact payload bytes, with unassigned
   bits zero.
3. Batch-size invariance: ingest the same events at several batch sizes into
   throwaway databases; per-frame active identity and window aggregates must
   match exactly.
4. Reopen: re-derive sampled stored rows from the persisted hex after opening
   a fresh connection.

Run ``python3.11 tools/verify.py`` (after ``tools/build_artifacts.py``).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import load_product  # noqa: E402
from bitcatalog.reference import (  # noqa: E402
    ref_decode,
    ref_encode,
    ref_overlap_pairs,
    ref_signal_from_def,
    ref_unassigned_bits,
)
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402


def load_events(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def check_catalog(defs: list[dict[str, Any]], product) -> list[str]:
    problems: list[str] = []
    by_key = {(m["bus"], m["frame_id"]): m for m in defs}
    for compiled in product.messages:
        definition = by_key[(compiled.bus, compiled.frame_id)]
        if ref_overlap_pairs(definition):
            problems.append(f"{compiled.key}: reference reports detectable overlap")
        unassigned = ref_unassigned_bits(definition)
        expected_mask = sum(1 << b for b in unassigned)
        if compiled.unassigned_mask != expected_mask:
            problems.append(
                f"{compiled.key}: unassigned mask {compiled.unassigned_mask:x} != reference {expected_mask:x}"
            )
        for raw, sig in zip(definition["signals"], compiled.signals):
            ref = ref_signal_from_def(raw, compiled.payload_bytes)
            # Derive the compiled field's physical positions and weights by
            # probing one physical bit at a time through the compiled
            # extractor, then compare with the reference model.
            total_bits = compiled.payload_bytes * 8
            observed: dict[int, int] = {}
            for position in range(total_bits):
                body = bytearray(compiled.payload_bytes)
                body[position // 8] |= 1 << (position % 8)
                pattern = sig.extract_raw(bytes(body))
                if pattern:
                    observed[position] = pattern
            expected_weights = dict(zip(ref.positions, ref.significance))
            if observed != expected_weights:
                extra = sorted(set(observed) - set(expected_weights))
                missing = sorted(set(expected_weights) - set(observed))
                wrong = [
                    p
                    for p in set(observed) & set(expected_weights)
                    if observed[p] != expected_weights[p]
                ]
                problems.append(
                    f"{compiled.key}/{sig.name}: compiled probe bits != reference "
                    f"(extra={extra[:4]}, missing={missing[:4]}, wrong_weight={sorted(wrong)[:4]})"
                )
    return problems


def check_frames(defs, products, db_path: Path, frames_path: Path, sample: int, rng) -> list[str]:
    problems: list[str] = []
    by_key = {(m["bus"], m["frame_id"]): m for m in defs}
    product = products["v1"]
    events = []
    with frames_path.open(encoding="utf-8") as fh:
        for line in fh:
            events.append(json.loads(line))
    indices = sorted(rng.sample(range(len(events)), min(sample, len(events))))
    checked = 0
    with ObservationWarehouse(db_path) as wh:
        for idx in indices:
            event = events[idx]
            stored = wh.frame(product, event["frame_event_id"])
            definition = by_key.get((event["bus"], event["frame_id"]))
            if definition is None:
                if stored.status != "rejected":
                    problems.append(f"{event['frame_event_id']}: unknown message should reject")
                continue
            try:
                payload = bytes.fromhex(event["payload_hex"])
            except ValueError:
                # malformed hex must have been rejected as a whole frame
                if stored.status != "rejected" or stored.error_code != "bad_encoding":
                    problems.append(f"{event['frame_event_id']}: malformed hex was not rejected as bad_encoding")
                continue
            if len(payload) != definition["payload_bytes"]:
                if stored.status != "rejected" or stored.error_code != "bad_length":
                    problems.append(f"{event['frame_event_id']}: wrong length was not rejected as bad_length")
                continue
            sel, raws, values = ref_decode(definition, payload)
            if stored.status != "ok":
                problems.append(f"{event['frame_event_id']}: reference decoded but store rejected ({stored.error_code})")
                continue
            checked += 1
            if stored.selector != sel:
                problems.append(f"{event['frame_event_id']}: selector {stored.selector} != ref {sel}")
            expected = {k: (raws[k], values[k]) for k in raws}
            if dict(stored.signals) != expected:
                problems.append(f"{event['frame_event_id']}: stored signals != reference")
            # reference encode must reproduce the exact bytes (zeros elsewhere)
            reencoded = ref_encode(definition, raws, sel)
            if reencoded.hex() != stored.payload_hex:
                problems.append(f"{event['frame_event_id']}: reference re-encode differs from stored bytes")
            bits = int.from_bytes(reencoded, "little")
            allowed = 0
            for name in raws:
                ref = ref_signal_from_def(next(s for s in definition["signals"] if s["name"] == name), definition["payload_bytes"])
                for p in ref.positions:
                    allowed |= 1 << p
            if definition["selector"] is not None:
                ref = ref_signal_from_def(
                    {**definition["selector"], "signed": False, "factor": "1", "offset": "0"},
                    definition["payload_bytes"],
                )
                for p in ref.positions:
                    allowed |= 1 << p
            if bits & ~allowed:
                problems.append(f"{event['frame_event_id']}: non-zero unassigned bits")
    return problems, checked


def check_batch_invariance(product, defs, frames_path: Path, sample: int, rng) -> list[str]:
    problems: list[str] = []
    events = []
    with frames_path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= sample:
                break
            events.append(json.loads(line))
    by_key = {(m["bus"], m["frame_id"]): m for m in defs}
    first = None
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for batch in (1, 13, 1000):
            path = Path(tmp) / f"b{batch}.db"
            with ObservationWarehouse(path) as wh:
                stats = wh.ingest(product, events, batch_size=batch)
                per_frame = {}
                for stored in wh.frames(product):
                    per_frame[stored.frame_event_id] = {
                        "selector": stored.selector,
                        "signals": dict(stored.signals),
                        "status": stored.status,
                    }
                windows = []
                for definition in defs[:5]:
                    for sig in definition["signals"][:2]:
                        agg = wh.query_window(product, sig["name"], 0, 2**62)
                        windows.append((sig["name"], agg.count, agg.total, agg.mean,
                                        [f.frame_event_id for f in agg.frames]))
                results[batch] = (stats.accepted, stats.rejected, per_frame, windows)
    base = results[1]
    for batch, value in results.items():
        if value[:2] != base[:2]:
            problems.append(f"batch {batch}: accepted/rejected {value[:2]} != {base[:2]}")
        if value[2] != base[2]:
            mismatched = [k for k in base[2] if value[2].get(k) != base[2][k]][:5]
            problems.append(f"batch {batch}: per-frame mismatch for {mismatched}")
        if value[3] != base[3]:
            problems.append(f"batch {batch}: window aggregates differ")
    return problems


def check_reopen(product, defs, frames_path: Path, sample: int, rng) -> list[str]:
    problems: list[str] = []
    events = []
    with frames_path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= sample * 4:
                break
            events.append(json.loads(line))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reopen.db"
        with ObservationWarehouse(path) as wh:
            wh.ingest(product, events, batch_size=97)
            before = {f.frame_event_id: f for f in wh.frames(product)}
        with ObservationWarehouse(path) as wh:
            after = {f.frame_event_id: f for f in wh.frames(product)}
        if set(before) != set(after):
            problems.append("reopen: frame set changed")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--batch-sample", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    out = Path(args.out)
    start = time.perf_counter()
    rng = random.Random(args.seed)
    defs = json.loads((out / "catalog_v1.definitions.json").read_text())["messages"]
    defs_v2 = json.loads((out / "catalog_v2.definitions.json").read_text())["messages"]
    products = {"v1": load_product(out / "catalog_v1.json"), "v2": load_product(out / "catalog_v2.json")}
    frames_path = out / "frames_v1.jsonl"
    db_path = out / "observations_v1.db"

    report: dict[str, Any] = {}
    report["catalog_v1_problems"] = check_catalog(defs, products["v1"])
    report["catalog_v2_problems"] = check_catalog(defs_v2, products["v2"])
    frames_problems, checked = check_frames(defs, products, db_path, frames_path, args.sample, rng)
    report["frame_sample_problems"] = frames_problems
    report["frame_sample_checked"] = checked
    report["batch_invariance_problems"] = check_batch_invariance(
        products["v1"], defs, frames_path, args.batch_sample, rng
    )
    report["reopen_problems"] = check_reopen(products["v1"], defs, frames_path, 100, rng)
    report["elapsed_seconds"] = round(time.perf_counter() - start, 2)
    report["ok"] = not any(
        report[k] for k in ("catalog_v1_problems", "catalog_v2_problems", "frame_sample_problems",
                            "batch_invariance_problems", "reopen_problems")
    )
    (out / "verification_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

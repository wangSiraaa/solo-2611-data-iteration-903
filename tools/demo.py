"""Interactive walkthrough of the catalog codec and observation warehouse.

Run ``python3.11 tools/demo.py`` after ``tools/build_artifacts.py``.  The demo
uses the published products and the actual warehouse under ``artifacts/`` and
prints every intermediate value, so the behaviour is inspectable rather than
asserted silently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product, fraction_text, load_product  # noqa: E402
from bitcatalog.reference import ref_decode  # noqa: E402
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402


CROSS_ORDER = {
    "bus": "demo",
    "frame_id": 1,
    "payload_bytes": 6,
    "selector": {"name": "mode", "start": 0, "width": 3, "order": "little"},
    "signals": [
        {"name": "little12_neg", "start": 3, "width": 12, "order": "little", "signed": True,
         "factor": "1/8", "offset": "-2"},
        {"name": "big13", "start": [2, 0], "width": 13, "order": "big", "signed": False,
         "factor": "3/7", "offset": "100"},
        {"name": "short", "start": 32, "width": 8, "order": "little", "signed": False,
         "factor": "1/10", "offset": "0", "branches": [0]},
        {"name": "wide", "start": 32, "width": 16, "order": "little", "signed": True,
         "factor": "-1/100", "offset": "5", "branches": [1, 2]},
    ],
}


def header(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def demo_codec() -> None:
    header("1. catalog publish + bit-field codec (cross-byte, both orders, signed)")
    product = build_product([CROSS_ORDER])
    print(f"product sha256 = {product.fingerprint}")
    compiled = product.messages[0]
    raws = {"little12_neg": -1000, "big13": 5000, "short": 7}
    frame = compiled.encode(raws, selector=0)
    print(f"raw values      = {raws} (selector 0)")
    print(f"payload bytes   = {frame.payload_hex}")
    sel, ref_raws, ref_values = ref_decode(CROSS_ORDER, frame.payload)
    print(f"reference decode= selector {sel}, raw {ref_raws}")
    print(f"reference values= { {k: fraction_text(v) for k, v in ref_values.items()} }")
    print(f"compiled values = { {s.name: s.value_text() for s in compiled.decode(frame.payload).signals} }")
    print(f"unassigned bits = {compiled.unassigned_mask:048b}")
    # show which physical bits each field owns
    for sig in compiled.signals:
        positions = [p for p in range(compiled.payload_bytes * 8) if (sig.mask << (sig.bit_offset if sig.order == 'big' else sig.start)) >> p & 1]
        print(f"  {sig.name:14s} {sig.order:6s} width={sig.width:2d} physical bits {positions}")
    # negative minimum
    lo = compiled.encode({"little12_neg": -2048, "big13": 0, "short": 0}, selector=0)
    dec = compiled.decode(lo.payload).by_name()["little12_neg"]
    print(f"signed min raw=-2048 -> payload {lo.payload_hex} -> raw {dec.raw}, value {dec.value_text()}")


def demo_warehouse(out: Path) -> None:
    header("2. warehouse bound to product v1: ingest, rejection, windows")
    db = out / "observations_v1.db"
    if not db.exists():
        print(f"missing {db}; run tools/build_artifacts.py first")
        return
    product = load_product(out / "catalog_v1.json")
    product_v2 = load_product(out / "catalog_v2.json")
    with ObservationWarehouse(db) as wh:
        counts = wh.counts(product)
        print(f"counts v1: {counts}")
        frames_path = out / "frames_v1.jsonl"
        events = []
        with frames_path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 8:
                    break
                events.append(json.loads(line))
        for event in events[:4]:
            stored = wh.frame(product, event["frame_event_id"])
            print(f"  {stored.frame_event_id}: status={stored.status} selector={stored.selector} "
                  f"bytes={stored.payload_hex[:24]}... signals={len(stored.signals)}")
            if stored.status != "ok":
                print(f"    rejected code={stored.error_code} position={stored.error_position}: {stored.error_message}")
        rejected = list(wh.frames(product, status="rejected", limit=3))
        for stored in rejected:
            print(f"  rejection {stored.frame_event_id}: code={stored.error_code} position={stored.error_position} "
                  f"payload={stored.payload_hex[:20]}")
        first = events[0]
        definition = next(m for m in product.content["messages"] if m["bus"] == first["bus"] and m["frame_id"] == first["frame_id"])
        signal = definition["signals"][0]["name"]
        start = min(e["occurred_at"] for e in events)
        agg = wh.query_window(product, signal, start, start + 5_000_000_000, bus=first["bus"], frame_id=first["frame_id"])
        print(f"window [{start}, +5s) for {signal!r}: count={agg.count} "
              f"sum={agg.total_text()} mean={agg.mean_text()}")
        print(f"  first source frames: {[f.frame_event_id for f in agg.frames[:3]]}")

        header("3. revision: same raw bytes, different signals")
        for event in events[:3]:
            old = wh.frame(product, event["frame_event_id"])
            new = wh.frame(product_v2, event["frame_event_id"])
            if old.status != "ok":
                continue
            print(f"  {event['frame_event_id']} bytes={old.payload_hex[:20]}...")
            print(f"    v1: {[(k, fraction_text(v)) for k, (_, v) in list(old.signals.items())[:2]]}")
            print(f"    v2: {[(k, fraction_text(v)) for k, (_, v) in list(new.signals.items())[:2]]}")
        print("  v1 aggregates are still queryable after v2 was published:")
        for db_product, sig in ((product, signal), (product_v2, "r2_" + signal)):
            names = wh.signal_names(db_product)
            if sig not in names:
                print(f"    {sig!r} is not present in the selected product (prefix r2_ for v2)")
                continue
            agg = wh.query_window(db_product, sig, 0, 2**62)
            print(f"    {db_product.fingerprint[:12]} {sig}: count={agg.count} mean={agg.mean_text()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    args = parser.parse_args(argv)
    demo_codec()
    demo_warehouse(Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

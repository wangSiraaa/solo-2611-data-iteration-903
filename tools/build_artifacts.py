"""End-to-end build: publish catalogs, generate events, fill the warehouse, report.

Outputs (under ``artifacts/`` unless overridden):

* ``catalog_v1.json`` / ``catalog_v2.json``          -- publishable products
* ``catalog_v1.definitions.json`` / ``_v2``          -- source definitions
* ``frames_v1.jsonl``                                -- raw bus events (hex bodies)
* ``observations_v1.db``                             -- the actual data warehouse
* ``build_report.json``                              -- sizes, timings, rejections,
                                                        sample aggregates and revision
                                                        comparison
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product, fraction_text  # noqa: E402
from bitcatalog.reference import ref_decode  # noqa: E402
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402
from tools.generate_dataset import (  # noqa: E402
    BASE_NS,
    build_catalog,
    make_events,
    revision_catalog,
)


def _load_jsonl(path: Path, limit: int | None = None):
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit is not None and i >= limit:
                break
            yield json.loads(line)


def _pick_samples(defs: list[dict[str, Any]], events: list[dict[str, Any]], k: int = 6):
    """Pick a few valid events spanning branch groups and both bit orders."""

    by_key = {(m["bus"], m["frame_id"]): m for m in defs}
    chosen = []
    seen_tags: set[tuple[bool, int | None, bool]] = set()
    for event in events:
        if event.get("_kind", "valid") != "valid":
            continue
        key = (event["bus"], event["frame_id"])
        if key not in by_key:
            continue
        definition = by_key[key]
        muxed = definition["selector"] is not None
        has_big = any(s["order"] == "big" for s in definition["signals"])
        selector = 0 if len(seen_tags) == 0 else 1 if len(seen_tags) == 1 else 2
        tag = (muxed, selector if muxed else None, has_big)
        if tag in seen_tags:
            continue
        seen_tags.add(tag)
        chosen.append(event)
        if len(chosen) >= k:
            break
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    parser.add_argument("--messages", type=int, default=200)
    parser.add_argument("--frames", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--skip-regenerate",
        action="store_true",
        help="reuse existing frames_v1.jsonl and catalog files",
    )
    parser.add_argument(
        "--scratch-dir",
        default=None,
        help="local directory for building the SQLite warehouse before copying it to --out",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"out": str(out), "seed": args.seed, "batch_size": args.batch_size}
    timings: dict[str, float] = {}

    def timed(name: str):
        class _T:
            def __enter__(self):
                self.t = time.perf_counter()
                return self

            def __exit__(self, *exc):
                timings[name] = round(time.perf_counter() - self.t, 3)

        return _T()

    # 1. publish catalogs -------------------------------------------------
    if args.skip_regenerate and (out / "catalog_v1.json").exists():
        from bitcatalog.catalog import load_product

        t0 = time.perf_counter()
        product = load_product(out / "catalog_v1.json")
        product_v2 = load_product(out / "catalog_v2.json")
        timings["load_products"] = round(time.perf_counter() - t0, 3)
        defs = json.loads((out / "catalog_v1.definitions.json").read_text())["messages"]
        defs_v2 = json.loads((out / "catalog_v2.definitions.json").read_text())["messages"]
    else:
        with timed("build_products"):
            defs = build_catalog(args.messages)
            defs_v2 = revision_catalog(defs, args.messages)
            product = build_product(defs)
            product_v2 = build_product(defs_v2)
            product.save(out / "catalog_v1.json")
            product_v2.save(out / "catalog_v2.json")
            (out / "catalog_v1.definitions.json").write_text(
                json.dumps({"protocol": product.content["protocol"], "messages": defs}, indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (out / "catalog_v2.definitions.json").write_text(
                json.dumps({"protocol": product_v2.content["protocol"], "messages": defs_v2}, indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    report["product_v1"] = {
        "sha256": product.fingerprint,
        "messages": len(product.messages),
        "signals": sum(len(m.signals) for m in product.messages),
        "bytes": (out / "catalog_v1.json").stat().st_size,
    }
    report["product_v2"] = {
        "sha256": product_v2.fingerprint,
        "messages": len(product_v2.messages),
    }

    # 2. generate events --------------------------------------------------
    frames_path = out / "frames_v1.jsonl"
    if not (args.skip_regenerate and frames_path.exists()):
        with timed("generate_events"):
            events, kinds = make_events(defs, product, args.frames, args.seed)
            with frames_path.open("w", encoding="utf-8") as fh:
                for event in events:
                    fh.write(json.dumps({k: v for k, v in event.items() if k != "_kind"}, sort_keys=True) + "\n")
        report["event_kinds"] = kinds
    else:
        report["event_kinds"] = "reused"

    # 3. ingest -----------------------------------------------------------
    # SQLite page writes are pathologically slow on some network/overlay
    # mounts, so the warehouse is built in a local scratch directory and the
    # finished database is copied to the deliverable path.  ``--scratch-dir``
    # pins that directory when the default temp volume is not local either.
    db_path = out / "observations_v1.db"
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db_path) + suffix)
        if target.exists():
            target.unlink()
    scratch = Path(tempfile.mkdtemp(prefix="bitcatalog-", dir=args.scratch_dir))
    work_db = scratch / "observations_v1.db"
    ingest_stats = {}
    with timed("ingest_v1"):
        with ObservationWarehouse(work_db) as wh:
            stats = wh.ingest(product, _load_jsonl(frames_path), batch_size=args.batch_size)
            ingest_stats["v1"] = stats.as_json()
            counts_v1 = wh.counts(product)
    with timed("ingest_v2"):
        with ObservationWarehouse(work_db) as wh:
            stats2 = wh.ingest(product_v2, _load_jsonl(frames_path), batch_size=args.batch_size)
            ingest_stats["v2"] = stats2.as_json()
    # checkpoint WAL and move the finished database into the deliverable path
    with ObservationWarehouse(work_db) as wh:
        wh.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        leftover = Path(str(work_db) + suffix)
        if leftover.exists():
            leftover.unlink()
    shutil.copyfile(work_db, db_path)
    report["ingest"] = ingest_stats
    report["counts_v1"] = counts_v1
    report["db_bytes"] = db_path.stat().st_size

    # 4. sample aggregates + revision comparison + re-derive from hex -----
    samples: list[dict[str, Any]] = []
    first_ns = BASE_NS
    last_ns = BASE_NS + args.frames * 3_000_000
    with ObservationWarehouse(db_path) as wh:
        all_events = list(_load_jsonl(frames_path, limit=200))
        picked = _pick_samples(defs, all_events, k=6)
        by_def = {(m["bus"], m["frame_id"]): m for m in defs}
        with timed("sample_queries"):
            for event in picked:
                definition = by_def[(event["bus"], event["frame_id"])]
                signal = definition["signals"][0]["name"]
                agg = wh.query_window(product, signal, first_ns, last_ns, bus=event["bus"], frame_id=event["frame_id"])
                stored = wh.frame(product, event["frame_event_id"])
                sel, raws, values = ref_decode(definition, bytes.fromhex(event["payload_hex"]))
                samples.append(
                    {
                        "frame_event_id": event["frame_event_id"],
                        "message": [event["bus"], event["frame_id"]],
                        "selector": sel,
                        "payload_hex": event["payload_hex"],
                        "reference_signals": {k: fraction_text(v) for k, v in values.items()},
                        "stored_signals": {k: [raw, fraction_text(val)] for k, (raw, val) in stored.signals.items()},
                        "reference_matches_store": {
                            k: (raws[k] == stored.signals[k][0] and values[k] == stored.signals[k][1])
                            for k in raws
                        },
                        "window": {
                            "signal": signal,
                            "count": agg.count,
                            "sum": agg.total_text(),
                            "mean": agg.mean_text(),
                            "first_frame": agg.frames[0].frame_event_id if agg.frames else None,
                            "last_frame": agg.frames[-1].frame_event_id if agg.frames else None,
                        },
                    }
                )
            # revision: identical bytes, different interpretation
            revision = []
            for i in range(min(5, len(all_events))):
                event = all_events[i]
                old = wh.frame(product, event["frame_event_id"])
                new = wh.frame(product_v2, event["frame_event_id"])
                if old.status != "ok":
                    continue
                revision.append(
                    {
                        "frame_event_id": event["frame_event_id"],
                        "payload_hex": old.payload_hex,
                        "v1": {k: fraction_text(v) for k, (_, v) in old.signals.items()},
                        "v2": {k: fraction_text(v) for k, (_, v) in new.signals.items()},
                    }
                )
            report["revision_samples"] = revision
        # reopen and re-derive stored signals from original hex
        wh.close()
    with ObservationWarehouse(db_path) as wh:
        with timed("reopen_rederive"):
            rederived = 0
            mismatches = 0
            for event in _load_jsonl(frames_path, limit=500):
                stored = wh.frame(product, event["frame_event_id"])
                if stored.status != "ok":
                    continue
                decoded = product.message(event["bus"], event["frame_id"]).decode(bytes.fromhex(stored.payload_hex))
                rebuilt = {s.name: (s.raw, s.value) for s in decoded.signals}
                rederived += 1
                if rebuilt != dict(stored.signals):
                    mismatches += 1
    report["reopen_rederive"] = {"frames": rederived, "mismatches": mismatches}
    report["samples"] = samples
    report["timings_seconds"] = timings
    report["frame_count"] = sum(1 for _ in frames_path.open()) if frames_path.exists() else 0
    shutil.rmtree(scratch, ignore_errors=True)

    (out / "build_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "product_v1": report["product_v1"],
                "event_kinds": report["event_kinds"],
                "ingest": {k: {kk: vv for kk, vv in v.items() if kk != "rejections"} for k, v in report["ingest"].items()},
                "counts_v1": report["counts_v1"],
                "reopen_rederive": report["reopen_rederive"],
                "timings_seconds": timings,
                "db_bytes": report["db_bytes"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

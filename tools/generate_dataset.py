"""Deterministic synthetic dataset generator (200 messages x 50 signals, 100k frames).

The generator owns *test data*, not the protocol semantics: it uses the
catalog codec to produce payloads from raw values and writes the events plus
the catalog definitions.  Tests decode the emitted hex with the independent
bit-list reference model in :mod:`bitcatalog.reference`.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product, fraction_text  # noqa: E402

SMALL_WIDTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
BIG_EXTRA = {5: 20, 17: 24, 29: 28, 41: 32}
BRANCH_SETS = [(0,), (1,), (2,), (3,), (0, 1), (2, 3)]
FACTORS = [
    (1, 1),
    (1, 10),
    (1, 100),
    (3, 8),
    (-1, 4),
    (5, 2),
    (1, 1000),
    (7, 3),
]
OFFSETS = [(0, 1), (-40, 1), (100, 1), (-1, 2), (3, 4), (200, 1), (-1, 1000)]
BASE_NS = 1_700_000_000_000_000_000


def _positions(order: str, start: Any, width: int) -> list[int]:
    if order == "little":
        return [start + k for k in range(width)]
    byte0, bit0 = start
    out = []
    for p in range(width):
        off = bit0 + p
        out.append((byte0 + off // 8) * 8 + (7 - off % 8))
    return out


def _conflict(scope_a: frozenset[int] | None, scope_b: frozenset[int] | None) -> bool:
    if scope_a is None or scope_b is None:
        return True
    return bool(scope_a & scope_b)


class Allocator:
    def __init__(self, payload_bytes: int):
        self.payload_bytes = payload_bytes
        self.placed: list[tuple[frozenset[int] | None, set[int]]] = []
        self._occupied = None  # lazily rebuilt full-set cache for uncoupled scope

    def _free(self, scope: frozenset[int] | None, positions: list[int]) -> bool:
        if positions[0] < 0 or positions[-1] >= self.payload_bytes * 8:
            return False
        pos = set(positions)
        if scope is None:
            if self._occupied is None:
                occ: set[int] = set()
                for _, bits in self.placed:
                    occ |= bits
                self._occupied = occ
            return not (self._occupied & pos)
        for other_scope, bits in self.placed:
            if _conflict(scope, other_scope) and not pos.isdisjoint(bits):
                return False
        return True

    def place(self, scope: frozenset[int] | None, width: int, order: str) -> tuple[str, Any]:
        if order == "little":
            for start in range(0, self.payload_bytes * 8 - width + 1):
                pos = list(range(start, start + width))
                if self._free(scope, pos):
                    self.placed.append((scope, set(pos)))
                    self._occupied = None
                    return "little", start
        else:
            for byte in range(self.payload_bytes):
                for bit in range(8):
                    pos = _positions("big", (byte, bit), width)
                    if self._free(scope, pos):
                        self.placed.append((scope, set(pos)))
                        self._occupied = None
                        return "big", [byte, bit]
        raise RuntimeError(f"cannot place width {width} {order} in {self.payload_bytes} bytes")

    def place_selector(self, width: int) -> int:
        pos = list(range(width))
        self.placed.append((None, set(pos)))
        self._occupied = None
        return 0

    def unassigned(self) -> set[int]:
        used: set[int] = set()
        for _, bits in self.placed:
            used |= bits
        return set(range(self.payload_bytes * 8)) - used


def _message_spec(index: int) -> dict[str, Any]:
    bus = "canA" if index % 2 == 0 else "canB"
    frame_id = 1000 + index
    muxed = index % 2 == 0
    payload_bytes = 64 if muxed else 48
    alloc = Allocator(payload_bytes)
    selectors = None
    if muxed:
        sel_width = 4
        sel_start = alloc.place_selector(sel_width)
        selectors = {"name": f"m{index:03d}_sel", "start": sel_start, "width": sel_width, "order": "little"}

    widths: list[int] = []
    for j in range(50):
        width = SMALL_WIDTHS[j % len(SMALL_WIDTHS)]
        if muxed and j in BIG_EXTRA:
            width = BIG_EXTRA[j]
        widths.append(width)

    signals = []
    for j, width in enumerate(widths):
        order = "little" if j % 2 == 0 else "big"
        if muxed and j >= 20:
            branches = BRANCH_SETS[(j - 20) % len(BRANCH_SETS)]
            scope: frozenset[int] | None = frozenset(branches)
        else:
            branches = ()
            scope = None
        try:
            placed_order, start = alloc.place(scope, width, order)
        except RuntimeError:
            # Layout is constructed to fit; fall back once for resilience on
            # unusually fragmented placements.
            placed_order, start = alloc.place(scope, width, "big" if order == "little" else "little")
        num, den = FACTORS[(index + j) % len(FACTORS)]
        onum, oden = OFFSETS[(index * 3 + j) % len(OFFSETS)]
        signals.append(
            {
                "name": f"m{index:03d}_s{j:02d}",
                "start": start,
                "width": width,
                "order": placed_order,
                "signed": j % 3 == 0,
                "factor": fraction_text(Fraction(num, den)),
                "offset": fraction_text(Fraction(onum, oden)),
                **({"branches": list(branches)} if branches else {}),
            }
        )
    return {
        "bus": bus,
        "frame_id": frame_id,
        "payload_bytes": payload_bytes,
        "selector": selectors,
        "signals": signals,
    }


def build_catalog(count: int = 200) -> list[dict[str, Any]]:
    return [_message_spec(i) for i in range(count)]


def revision_catalog(base: list[dict[str, Any]], count: int = 200) -> list[dict[str, Any]]:
    """A published revision: same layouts, re-interpreted signals for 40 messages."""

    out = json.loads(json.dumps(base))
    for i in range(0, min(40, count)):
        msg = out[i]
        for j, sig in enumerate(msg["signals"]):
            sig["name"] = f"r2_{sig['name']}"
            # Change the rational scaling so even identical bytes produce
            # observably different values.
            num, den = sig["factor"].split("/") if "/" in sig["factor"] else (sig["factor"], "1")
            sig["factor"] = fraction_text(Fraction(int(num), int(den)) * 2)
        if msg["selector"] is not None:
            msg["selector"]["name"] = f"r2_{msg['selector']['name']}"
    return out


def _raw_range(sig: dict[str, Any]) -> tuple[int, int]:
    width = sig["width"]
    if sig["signed"]:
        return -(1 << (width - 1)), (1 << (width - 1)) - 1
    return 0, (1 << width) - 1


def make_events(
    definitions: list[dict[str, Any]],
    product,
    frame_count: int,
    seed: int = 20261118,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rng = random.Random(seed)
    by_def = {(m.bus, m.frame_id): m for m in product.messages}
    events: list[dict[str, Any]] = []
    stats = {"valid": 0, "bad_hex": 0, "bad_length": 0, "unknown_message": 0}
    ns = BASE_NS
    for i in range(frame_count):
        definition = definitions[i % len(definitions)]
        msg = by_def[(definition["bus"], definition["frame_id"])]
        selector = None
        if msg.selector is not None:
            # Select only configured branch values; 0..3 covers every group.
            selector = i % 4
        raws: dict[str, int] = {}
        for sig in msg.signals:
            if msg.selector is not None:
                if sig.branches and selector not in sig.branches:
                    continue
            lo, hi = sig.raw_min, sig.raw_max
            # Force boundaries regularly so negative minima and width maxima
            # are exercised for every message over its ~500 frames.
            if i % 97 == 0:
                raw = lo
            elif i % 89 == 0:
                raw = hi
            else:
                raw = rng.randint(lo, hi)
            raws[sig.name] = raw
        frame_body, _, _ = msg.encode_body(raws, selector=selector)
        payload_hex = frame_body.hex()
        kind = "valid"
        frame_id = msg.frame_id
        bus = msg.bus
        if i % 997 == 3:
            kind = "bad_hex"
            if len(payload_hex) > 4:
                pos = rng.randrange(len(payload_hex))
                payload_hex = payload_hex[:pos] + "G" + payload_hex[pos + 1 :]
            else:
                payload_hex += "G"
        elif i % 991 == 5:
            kind = "bad_length"
            if len(payload_hex) >= 4:
                payload_hex = payload_hex[:-2]
            else:
                payload_hex += "00"
        elif i % 983 == 7:
            kind = "unknown_message"
            frame_id = 900000 + (i % 37)
            bus = "canA" if i % 2 else "canB"
        stats[kind] += 1
        ns += 1_000_000 + (i * 7919) % 1_000_000
        events.append(
            {
                "frame_event_id": f"evt-{i:08d}",
                "bus": bus,
                "frame_id": frame_id,
                "occurred_at_ns": ns,
                "occurred_at": ns,
                "payload_hex": payload_hex,
                "_kind": kind,
            }
        )
    return events, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    parser.add_argument("--messages", type=int, default=200)
    parser.add_argument("--frames", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

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

    events, stats = make_events(defs, product, args.frames, args.seed)
    with (out / "frames_v1.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    meta = {
        "seed": args.seed,
        "message_count": len(defs),
        "signal_count": sum(len(m["signals"]) for m in defs),
        "frame_count": len(events),
        "event_kinds": stats,
        "product_v1_sha256": product.fingerprint,
        "product_v2_sha256": product_v2.fingerprint,
        "base_ns": BASE_NS,
        "first_event_ns": events[0]["occurred_at_ns"],
        "last_event_ns": events[-1]["occurred_at_ns"],
    }
    (out / "dataset_manifest.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Warehouse tests: binding, whole-frame rejection, exact windows, revisions."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product  # noqa: E402
from bitcatalog.warehouse import (  # noqa: E402
    ObservationWarehouse,
    format_ns,
    parse_occurred_ns,
)
from bitcatalog.reference import ref_decode  # noqa: E402


def definition_v1():
    return {
        "bus": "b",
        "frame_id": 7,
        "payload_bytes": 4,
        "selector": {"name": "sel", "start": 0, "width": 3, "order": "little"},
        "signals": [
            {"name": "always_a", "start": 3, "width": 5, "order": "little", "signed": False,
             "factor": "1/2", "offset": "-1"},
            {"name": "always_b", "start": [1, 0], "width": 8, "order": "big", "signed": True,
             "factor": "1/4", "offset": "100"},
            {"name": "branch0", "start": 16, "width": 8, "order": "little", "signed": False,
             "factor": "1/10", "offset": "0", "branches": [0]},
            {"name": "branch1", "start": 16, "width": 8, "order": "little", "signed": False,
             "factor": "2", "offset": "1", "branches": [1]},
            {"name": "branch23", "start": 16, "width": 8, "order": "little", "signed": True,
             "factor": "-1/100", "offset": "5", "branches": [2, 3]},
        ],
    }


def definition_v2():
    """Same layout, re-interpreted signals and a different scaling."""

    d = json.loads(json.dumps(definition_v1()))
    for sig in d["signals"]:
        sig["name"] = "r2_" + sig["name"]
        num, den = (sig["factor"].split("/") if "/" in sig["factor"] else (sig["factor"], "1"))
        sig["factor"] = f"{int(num) * 10}/{den}"
    d["selector"]["name"] = "r2_sel"
    return d


BASE_NS = 1_700_000_000_000_000_000


def event(i, payload_hex, *, bus="b", frame_id=7, ns=None):
    return {
        "frame_event_id": f"e{i:04d}",
        "bus": bus,
        "frame_id": frame_id,
        "occurred_at_ns": BASE_NS + i * 1_000_000_000 if ns is None else ns,
        "payload_hex": payload_hex,
    }


class BindingAndRejection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "wh.db"
        self.wh = ObservationWarehouse(self.path)
        self.p = build_product([definition_v1()])
        self.c = self.p.messages[0]

    def tearDown(self):
        self.wh.close()
        self.tmp.cleanup()

    def test_bad_length_rejected_with_position(self):
        payload = self.c.encode({"always_a": 3, "always_b": 0, "branch0": 1}, selector=0).payload
        short = payload[:-1]
        long = payload + b"\x00"
        for bad in (short, long):
            stats = self.wh.ingest(self.p, [event(0, bad.hex(), ns=BASE_NS)])
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.accepted, 0)
            stored = self.wh.frame(self.p, "e0000")
            self.assertEqual(stored.status, "rejected")
            self.assertEqual(stored.error_code, "bad_length")
            self.assertEqual(stored.error_position, 4)
            self.assertEqual(stored.signals, {})

    def test_bad_hex_rejected_with_nibble_position(self):
        stats = self.wh.ingest(self.p, [event(0, "00zz0000")])
        self.assertEqual(stats.rejected, 1)
        stored = self.wh.frame(self.p, "e0000")
        self.assertEqual(stored.error_code, "bad_encoding")
        self.assertEqual(stored.error_position, 2)
        self.assertIn("'z'", stored.error_message)
        # odd nibble count
        self.wh.ingest(self.p, [event(1, "000")])
        self.assertEqual(self.wh.frame(self.p, "e0001").error_position, 3)

    def test_unknown_message_and_wrong_bus_rejected(self):
        payload = self.c.encode({"always_a": 3, "always_b": 0, "branch0": 1}, selector=0).payload.hex()
        self.wh.ingest(self.p, [event(0, payload, frame_id=999), event(1, payload, bus="other")])
        self.assertEqual(self.wh.frame(self.p, "e0000").error_code, "unknown_message")
        self.assertEqual(self.wh.frame(self.p, "e0001").error_code, "unknown_message")
        self.assertEqual(self.wh.counts(self.p), {"ok": 0, "rejected": 2, "total": 2, "observations": 0})

    def test_rejected_frame_keeps_position_and_no_observations(self):
        payload = b"\x01\x02\x03"  # wrong length
        self.wh.ingest(self.p, [event(0, payload.hex())])
        stored = self.wh.frame(self.p, "e0000")
        self.assertEqual(stored.payload, payload)
        self.assertEqual(stored.payload_hex, payload.hex())
        self.assertEqual(stored.signals, {})
        agg = self.wh.query_window(self.p, "always_a", 0, 2**62)
        self.assertEqual((agg.count, agg.total), (0, Fraction(0)))

    def test_frames_are_bound_to_product(self):
        other = build_product([definition_v2()])
        payload = self.c.encode({"always_a": 3, "always_b": 0, "branch0": 1}, selector=0).payload.hex()
        self.wh.ingest(self.p, [event(0, payload)])
        # v2 does not contain the v1 signal name at all
        v2 = self.wh.ingest(other, [event(0, payload)])
        self.assertEqual(v2.accepted, 1)
        self.assertEqual(self.wh.frame(self.p, "e0000").signals["always_a"], (3, Fraction(3, 2) - 1))
        self.assertEqual(self.wh.frame(other, "e0000").signals["r2_always_a"], (3, Fraction(15) - 1))
        self.assertEqual(len(self.wh.products()), 2)


class ActivityAndWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "wh.db"
        self.wh = ObservationWarehouse(self.path)
        self.p = build_product([definition_v1()])
        self.c = self.p.messages[0]

    def tearDown(self):
        self.wh.close()
        self.tmp.cleanup()

    def _payload(self, selector, a, b, br):
        raws = {"always_a": a, "always_b": b}
        name = {0: "branch0", 1: "branch1"}.get(selector, "branch23")
        raws[name] = br
        return self.c.encode(raws, selector=selector).payload.hex()

    def test_inactive_signals_have_no_row(self):
        self.wh.ingest(self.p, [event(0, self._payload(0, 1, 2, 3)), event(1, self._payload(1, 1, 2, 3))])
        f0 = self.wh.frame(self.p, "e0000")
        self.assertEqual(set(f0.signals), {"always_a", "always_b", "branch0"})
        self.assertNotIn("branch1", f0.signals)
        # not NULL, not zero: absent from the observations table entirely
        rows = self.wh.connection.execute(
            "SELECT s.name FROM observations o JOIN signals s ON s.id = o.signal_id"
            " JOIN frames f ON f.pk = o.frame_pk"
            " WHERE f.frame_event_id='e0000' AND s.product_id = o.product_id",
        ).fetchall()
        self.assertEqual({r[0] for r in rows}, {"always_a", "always_b", "branch0"})
        agg = self.wh.query_window(self.p, "branch0", 0, 2**62)
        self.assertEqual(agg.count, 1)
        agg = self.wh.query_window(self.p, "branch1", 0, 2**62)
        self.assertEqual(agg.count, 1)
        self.assertNotEqual(agg.frames[0].frame_event_id, "e0000")

    def test_half_open_window_and_exact_mean(self):
        # Three frames at t=0, 1s, 2s with always_a raw 0, 3, 7.
        # always_a factor 1/2 offset -1 -> -1, 1/2, 5/2
        self.wh.ingest(
            self.p,
            [
                event(0, self._payload(0, 0, 0, 0)),
                event(1, self._payload(0, 3, 0, 0)),
                event(2, self._payload(0, 7, 0, 0)),
            ],
        )
        t0 = BASE_NS
        t1 = BASE_NS + 1_000_000_000
        t2 = BASE_NS + 2_000_000_000
        agg = self.wh.query_window(self.p, "always_a", t0, t1)
        self.assertEqual(agg.count, 1)
        self.assertEqual(agg.total, Fraction(-1))
        self.assertEqual(agg.mean, Fraction(-1))
        self.assertEqual([f.frame_event_id for f in agg.frames], ["e0000"])
        agg = self.wh.query_window(self.p, "always_a", t1, t2)
        self.assertEqual((agg.count, agg.total), (1, Fraction(1, 2)))
        agg = self.wh.query_window(self.p, "always_a", t0, t2)  # excludes t2
        self.assertEqual(agg.count, 2)
        self.assertEqual(agg.total, Fraction(-1, 2))
        self.assertEqual(agg.mean, Fraction(-1, 4))
        self.assertEqual([f.frame_event_id for f in agg.frames], ["e0000", "e0001"])
        # empty half-open window
        agg = self.wh.query_window(self.p, "always_a", t2, t2)
        self.assertEqual((agg.count, agg.total, agg.mean), (0, Fraction(0), Fraction(0)))
        # include the last frame
        agg = self.wh.query_window(self.p, "always_a", t0, t2 + 1)
        self.assertEqual(agg.count, 3)
        self.assertEqual(agg.total, Fraction(-1) + Fraction(1, 2) + Fraction(5, 2))
        self.assertEqual(agg.mean, agg.total / 3)

    def test_duplicate_event_id_redelivery(self):
        payload_a = self._payload(0, 1, 2, 3)
        payload_b = self._payload(0, 5, 2, 3)
        self.wh.ingest(self.p, [event(0, payload_a)])
        stats = self.wh.ingest(self.p, [event(0, payload_b)])
        self.assertEqual(stats.replaced, 1)
        self.assertEqual(self.wh.counts(self.p)["total"], 1)
        stored = self.wh.frame(self.p, "e0000")
        self.assertEqual(stored.payload_hex, payload_b)
        self.assertEqual(stored.signals["always_a"][0], 5)
        # ignore leaves the first row untouched
        self.wh.ingest(self.p, [event(0, payload_a)], on_duplicate="ignore")
        self.assertEqual(self.wh.frame(self.p, "e0000").payload_hex, payload_b)
        with self.assertRaises(Exception):
            self.wh.ingest(self.p, [event(0, payload_a)], on_duplicate="error")

    def test_bus_and_frame_filters(self):
        self.wh.ingest(self.p, [event(0, self._payload(0, 1, 2, 3), bus="b")])
        alloc = self.p.messages[0]
        other = build_product([{**definition_v1(), "bus": "c", "frame_id": 8}])
        self.wh.ingest(other, [event(1, self._payload(0, 3, 2, 3), bus="c", frame_id=8)])
        all_ = self.wh.query_window(self.p, "always_a", 0, 2**62)
        self.assertEqual(all_.count, 1)
        filtered = self.wh.query_window(self.p, "always_a", 0, 2**62, bus="b", frame_id=7)
        self.assertEqual(filtered.count, 1)


class BatchInvariance(unittest.TestCase):
    def test_same_results_for_batch_sizes(self):
        rng = random.Random(99)
        product = build_product([definition_v1()])
        c = product.messages[0]
        events = []
        for i in range(600):
            sel = i % 4
            name = {0: "branch0", 1: "branch1"}.get(sel, "branch23")
            raws = {"always_a": rng.randint(0, 31), "always_b": rng.randint(-128, 127), name: rng.randint(-128, 127) if name == "branch23" else rng.randint(0, 255)}
            events.append(event(i, c.encode(raws, selector=sel).payload.hex(), ns=BASE_NS + i * 10_000_000))
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            for batch in (1, 7, 64, 5000):
                path = Path(tmp) / f"wh_{batch}.db"
                with ObservationWarehouse(path) as wh:
                    stats = wh.ingest(product, events, batch_size=batch)
                    self.assertEqual(stats.accepted, 600)
                    per_frame = {}
                    for stored in wh.frames(product):
                        per_frame[stored.frame_event_id] = {
                            name: (raw, value) for name, (raw, value) in stored.signals.items()
                        }
                    windows = {
                        sig: wh.query_window(product, sig, BASE_NS, BASE_NS + 600 * 10_000_000)
                        for sig in ("always_a", "always_b", "branch0", "branch1", "branch23")
                    }
                    results.append((per_frame, windows))
        base_frames, base_windows = results[0]
        for per_frame, windows in results[1:]:
            self.assertEqual(per_frame, base_frames)
            for sig, agg in windows.items():
                self.assertEqual(agg.count, base_windows[sig].count)
                self.assertEqual(agg.total, base_windows[sig].total)
                self.assertEqual(agg.mean, base_windows[sig].mean)
                self.assertEqual(
                    [f.frame_event_id for f in agg.frames],
                    [f.frame_event_id for f in base_windows[sig].frames],
                )


class ReopenAndRevision(unittest.TestCase):
    def test_reopen_rebuild_from_hex_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wh.db"
            product_v1 = build_product([definition_v1()])
            product_v2 = build_product([definition_v2()])
            c1 = product_v1.messages[0]
            sample_hex = []
            rng = random.Random(5)
            events = []
            for i in range(50):
                sel = i % 4
                name = {0: "branch0", 1: "branch1"}.get(sel, "branch23")
                raws = {"always_a": rng.randint(0, 31), "always_b": rng.randint(-128, 127), name: rng.randint(-128, 127) if name == "branch23" else rng.randint(0, 255)}
                payload = c1.encode(raws, selector=sel).payload
                sample_hex.append(payload.hex())
                events.append(event(i, payload.hex(), ns=BASE_NS + i * 1_000_000))
            with ObservationWarehouse(path) as wh:
                wh.ingest(product_v1, events, batch_size=8)
                wh.ingest(product_v2, events, batch_size=8)
            with ObservationWarehouse(path) as wh:
                # old product result is still reviewable after reopen + v2 publish
                for i, hex_text in enumerate(sample_hex[:10]):
                    stored = wh.frame(product_v1, f"e{i:04d}")
                    self.assertEqual(stored.payload_hex, hex_text)
                    definition = definition_v1()
                    _, raws, values = ref_decode(definition, bytes.fromhex(stored.payload_hex))
                    self.assertEqual({k: v for k, v in stored.signals.items()}, {k: (raws[k], values[k]) for k in raws})
                # same bytes under v2 produce different signal names and values
                stored_v2 = wh.frame(product_v2, "e0000")
                stored_v1 = wh.frame(product_v1, "e0000")
                self.assertEqual(set(stored_v2.signals), {"r2_" + n for n in stored_v1.signals})
                self.assertNotEqual(
                    stored_v1.signals["always_a"][1],
                    stored_v2.signals["r2_always_a"][1],
                )
                agg1 = wh.query_window(product_v1, "always_a", 0, 2**62)
                agg2 = wh.query_window(product_v2, "r2_always_a", 0, 2**62)
                # v2 scales the factor by 10 but keeps the offset, so each
                # value maps as v2 = 10 * (v1 + 1) - 1.
                self.assertEqual(agg2.count, agg1.count)
                self.assertEqual(agg2.total, 10 * (agg1.total + agg1.count) - agg1.count)
                self.assertEqual(agg2.mean, 10 * (agg1.mean + 1) - 1)
                self.assertEqual(len(wh.products()), 2)
                # restoring the payload bytes yields the identical stored interpretation
                for i in range(10):
                    stored = wh.frame(product_v1, f"e{i:04d}")
                    decoded = c1.decode(bytes.fromhex(sample_hex[i]))
                    self.assertEqual(
                        {s.name: (s.raw, s.value) for s in decoded.signals},
                        dict(stored.signals),
                    )


class Timestamps(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(format_ns(0), "1970-01-01T00:00:00.000000000Z")
        self.assertEqual(format_ns(1), "1970-01-01T00:00:00.000000001Z")
        self.assertEqual(format_ns(BASE_NS), "2023-11-14T22:13:20.000000000Z")
        self.assertEqual(parse_occurred_ns("2023-11-14T22:13:20Z"), BASE_NS)
        self.assertEqual(parse_occurred_ns("2023-11-14T22:13:20.000000001Z"), BASE_NS + 1)
        self.assertEqual(parse_occurred_ns(BASE_NS), BASE_NS)
        with self.assertRaises(ValueError):
            parse_occurred_ns("yesterday")

    def test_ordering_matches_time(self):
        # format_ns must be lexicographically ordered for the same precision.
        values = [0, 1, 999_999_999, 1_000_000_000, BASE_NS, BASE_NS + 123]
        rendered = [format_ns(v) for v in values]
        self.assertEqual(rendered, sorted(rendered))


if __name__ == "__main__":
    unittest.main()

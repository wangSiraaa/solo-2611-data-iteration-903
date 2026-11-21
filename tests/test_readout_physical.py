"""Readout definitions, physical reconstruction and version binding."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product  # noqa: E402
from bitcatalog.physical import (  # noqa: E402
    Load,
    PhysicalLedger,
    explain_frame,
    physical_sheets_for_book,
    query_book,
)
from bitcatalog.readout import (  # noqa: E402
    ReadoutDefinition,
    ReadoutError,
    decode_warehouse,
    load_readout,
    publish_readout,
)
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402

DEFINITION = {
    "bus": "bindery",
    "frame_id": 10,
    "payload_bytes": 4,
    "selector": {"name": "act", "start": 0, "width": 3, "order": "little"},
    "signals": [
        {"name": "table", "start": 3, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "seq", "start": 11, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "book", "start": 19, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
    ],
}

ACTIONS = {"0": "pickup", "1": "empty_punch", "2": "pull_last", "3": "seal"}


def spec(**overrides):
    base = {
        "bus": "bindery",
        "frame_id": 10,
        "actions": ACTIONS,
        "table_signal": "table",
        "machine_seq_signal": "seq",
        "book_signal": "book",
        "selector_name": "act",
    }
    base.update(overrides)
    return base


def setup():
    product = build_product([DEFINITION])
    definition = ReadoutDefinition.parse({**spec(), "product_sha256": product.fingerprint})
    return product, definition, definition.compile(product)


def frame(codec, fid, selector, seq, *, book=0, table=1, arrival=None):
    payload = codec.message.encode({"table": table, "seq": seq, "book": book}, selector=selector).payload
    return codec.decode(payload, frame_event_id=fid, occurred_at_ns=seq if arrival is None else arrival)


class ReadoutPublishing(unittest.TestCase):
    def test_publish_load_and_fingerprint_guard(self):
        product, definition, _ = setup()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readout.json"
            publish_readout(product, spec(), path)
            loaded = load_readout(path, product=product)
            self.assertEqual(loaded.fingerprint, definition.fingerprint)
            manifest = json.loads(path.read_text())
            manifest["definition"]["actions"]["0"] = "seal"
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ReadoutError) as ctx:
                load_readout(path)
            self.assertEqual(ctx.exception.code, "bad_readout_hash")

    def test_readout_is_bound_to_product_version(self):
        product = build_product([DEFINITION])
        definition = ReadoutDefinition.parse({**spec(), "product_sha256": product.fingerprint})
        other = build_product([{**DEFINITION, "bus": "other"}])
        with self.assertRaises(ReadoutError) as ctx:
            definition.compile(other)
        self.assertEqual(ctx.exception.code, "wrong_product")

    def test_branch_only_or_signed_identifiers_rejected(self):
        product = build_product([DEFINITION])
        for bad in (
            spec(table_signal="act"),
            spec(machine_seq_signal="missing_signal"),
        ):
            definition = ReadoutDefinition.parse({**bad, "product_sha256": product.fingerprint})
            with self.assertRaises(ReadoutError):
                definition.compile(product)

    def test_unknown_action_rejected(self):
        with self.assertRaises(ReadoutError):
            ReadoutDefinition.parse({**spec(), "product_sha256": "0" * 64,
                                     "actions": {"0": "teleport"}})

    def test_decode_identifiers_are_raw(self):
        _, _, codec = setup()
        f = frame(codec, "x", 0, 7, book=3, table=5)
        self.assertEqual((f.table, f.machine_seq, f.book, f.action), (5, 7, 3, "pickup"))


class PhysicalWorkedExample(unittest.TestCase):
    def setUp(self):
        _, self.definition, self.codec = setup()
        self.loads = [Load("1", 0, "P1"), Load("1", 1, "P2")]

    def test_real_pickup_wastes_first_batch_and_seals_p2(self):
        frames = [
            frame(self.codec, "f0", 0, 1),  # pickup P1
            frame(self.codec, "f1", 2, 2),  # pull_last -> waste P1
            frame(self.codec, "f2", 0, 3),  # pickup P2
            frame(self.codec, "f3", 3, 4, book=1),  # seal
        ]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        book = result.books[0]
        self.assertEqual(book.sheet_tags, ("P2",))
        self.assertEqual(book.product_tag, "P2")
        self.assertTrue(book.short)
        report = physical_sheets_for_book(result, book.book_id)
        self.assertEqual(report["sheet_frames"], ["f2"])
        self.assertEqual(result.waste["1"], ["P1"])

    def test_empty_punch_first_yields_p1(self):
        frames = [
            frame(self.codec, "g0", 1, 1),  # empty punch
            frame(self.codec, "g1", 2, 2),  # pull_last: empty book -> no-op
            frame(self.codec, "g2", 0, 3),  # pickup P1
            frame(self.codec, "g3", 3, 4, book=1),  # seal
        ]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        book = result.books[0]
        self.assertEqual(book.sheet_tags, ("P1",))
        self.assertEqual(book.product_tag, "P1")
        self.assertFalse(book.short)
        self.assertEqual(explain_frame(result, "g1")["kind"], "noop")

    def test_cursor_does_not_rewind_on_pull(self):
        frames = [
            frame(self.codec, "h0", 0, 1),
            frame(self.codec, "h1", 2, 2),
            frame(self.codec, "h2", 0, 3),
        ]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        self.assertEqual(result.next_cursor["1"], 2)

    def test_empty_punch_does_not_advance(self):
        frames = [frame(self.codec, "i0", 1, 1), frame(self.codec, "i1", 1, 2)]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        self.assertEqual(result.next_cursor.get("1", 0), 0)

    def test_empty_book_pull_has_no_physical_action(self):
        frames = [frame(self.codec, "j0", 2, 1), frame(self.codec, "j1", 3, 2, book=1)]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        self.assertEqual(explain_frame(result, "j0")["kind"], "noop")
        self.assertEqual(result.waste, {})
        # seal with an empty book is blocked, not a phantom product
        self.assertEqual(result.books, [])


class LateDuplicateContradiction(unittest.TestCase):
    def setUp(self):
        _, self.definition, self.codec = setup()
        self.loads = [Load("1", 0, "P1"), Load("1", 1, "P2")]

    def test_late_pickup_reorders_by_machine_seq(self):
        # The seq-1 frame arrives last but must be replayed first.
        frames = [
            frame(self.codec, "late", 0, 1, arrival=99),
            frame(self.codec, "s2", 0, 2, arrival=2),
            frame(self.codec, "s3", 1, 3, arrival=3),
            frame(self.codec, "s4", 3, 4, book=1, arrival=4),
        ]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        # pickup P1, pickup P2, empty punch, seal -> P2
        self.assertEqual(result.books[0].product_tag, "P2")

    def test_duplicate_slot_applies_once(self):
        dup_a = frame(self.codec, "d0", 0, 1)
        dup_b = frame(self.codec, "d0-copy", 0, 1)
        frames = [dup_a, dup_b, frame(self.codec, "d1", 3, 2, book=1)]
        result = PhysicalLedger(self.loads).reconstruct(frames)
        self.assertEqual(result.books[0].product_tag, "P1")
        self.assertEqual(explain_frame(result, "d0")["kind"], "applied")
        self.assertEqual(explain_frame(result, "d0-copy")["status"], "duplicate")
        self.assertEqual(result.next_cursor["1"], 1)

    def test_contradiction_withholds_and_resolution_re_derives(self):
        conflict_a = frame(self.codec, "c0a", 0, 1)  # pickup
        conflict_b = frame(self.codec, "c0b", 1, 1)  # empty punch, same slot
        frames = [conflict_a, conflict_b, frame(self.codec, "c2", 0, 2),
                  frame(self.codec, "c3", 3, 3, book=1)]
        ledger = PhysicalLedger(self.loads)
        result = ledger.reconstruct(frames)
        self.assertEqual(result.unclear_seqs, [("bindery", 1, 1)])
        self.assertEqual(explain_frame(result, "c0a")["status"], "unclear")
        self.assertEqual(explain_frame(result, "c0b")["status"], "unclear")
        # Unresolved: neither frame applied, so the book is built from a single
        # later pickup -> unclear seal withheld from money (tested elsewhere).
        self.assertEqual(result.books[0].product_tag, "P1")

        ledger.resolve("bindery", 1, "c0a")  # choose the real pickup
        resolved = ledger.reconstruct(frames)
        self.assertEqual(resolved.unclear_seqs, [])
        # pickup P1, pickup P2, seal -> P2, matching the later evidence.
        self.assertEqual(resolved.books[0].product_tag, "P2")


    def test_late_pickup_changes_later_book_batch(self):
        # Table P1,P2,P3.  Two books seal in sequence:
        #   book 1: pickup, seal -> P1
        #   book 2: pickup, seal -> P2
        # A late pickup with an earlier machine sequence arrives afterwards.
        # It consumes P1 first, so book 1 becomes P2 and book 2 becomes P3.
        loads = [Load("1", 0, "P1"), Load("1", 1, "P2"), Load("1", 2, "P3")]
        base = [
            frame(self.codec, "k1", 0, 2),           # book 1 pickup
            frame(self.codec, "k2", 3, 3, book=1),   # book 1 seal
            frame(self.codec, "k4", 0, 4),           # book 2 pickup
            frame(self.codec, "k5", 3, 5, book=2),   # book 2 seal
        ]
        before = PhysicalLedger(loads).reconstruct(base)
        self.assertEqual([b.product_tag for b in before.books], ["P1", "P2"])

        late = base + [frame(self.codec, "k0", 0, 1, arrival=999)]
        after = PhysicalLedger(loads).reconstruct(late)
        self.assertEqual([b.product_tag for b in after.books], ["P2", "P3"])
        # The late frame is the one that moved both books.
        self.assertEqual(explain_frame(after, "k0")["batch"], "P1")


class WarehouseVersionedReadout(unittest.TestCase):
    def test_original_frames_queried_per_product_version(self):
        product = build_product([DEFINITION])
        definition = ReadoutDefinition.parse({**spec(), "product_sha256": product.fingerprint})
        codec = definition.compile(product)
        frames = [
            frame(codec, "f0", 0, 1),
            frame(codec, "f1", 2, 2),
            frame(codec, "f2", 0, 3),
            frame(codec, "f3", 3, 4, book=1),
        ]
        events = [
            {
                "frame_event_id": f.frame_event_id,
                "bus": f.bus,
                "frame_id": f.frame_id,
                "occurred_at_ns": f.occurred_at_ns,
                "payload_hex": f.payload_hex,
            }
            for f in frames
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "wh.db"
            with ObservationWarehouse(db) as wh:
                wh.ingest(product, events)
                decoded = decode_warehouse(wh, definition)
                # Malformed frames are not invented: only the stored event ids.
                self.assertEqual({f.frame_event_id for f in decoded}, {"f0", "f1", "f2", "f3"})
                result = PhysicalLedger([Load("1", 0, "P1"), Load("1", 1, "P2")]).reconstruct(decoded)
                report = query_book(result, result.books[0].book_id, decoded)
                self.assertEqual(report["product_sha256"], product.fingerprint)
                self.assertEqual([f["payload_hex"] for f in report["frames"]],
                                 [f.payload_hex for f in frames if f.frame_event_id in ("f2", "f3")])


if __name__ == "__main__":
    unittest.main()

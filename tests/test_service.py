"""End-to-end facade over a real warehouse: publish -> reconstruct -> claim."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product  # noqa: E402
from bitcatalog.contracts import Contract  # noqa: E402
from bitcatalog.physical import Load, PhysicalLedger  # noqa: E402
from bitcatalog.readout import ReadoutDefinition, publish_readout  # noqa: E402
from bitcatalog.service import BinderyService  # noqa: E402
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
        {"name": "speed", "start": 27, "width": 5, "order": "little", "signed": False,
         "factor": "3/2", "offset": "0"},
    ],
}
ACTIONS = {"0": "pickup", "1": "empty_punch", "2": "pull_last", "3": "seal"}


class Facade(unittest.TestCase):
    def test_publish_query_and_claim_through_service(self):
        product = build_product([DEFINITION])
        revision = build_product([{**DEFINITION, "signals": [
            {**s, "name": "r2_" + s["name"]} for s in DEFINITION["signals"]
        ], "selector": {**DEFINITION["selector"], "name": "r2_act"}}])
        codec = product.messages[0]

        def ev(i, sel, seq, *, book=0, table=1, arrival=None):
            payload = codec.encode({"table": table, "seq": seq, "book": book, "speed": 5},
                                   selector=sel).payload
            return {
                "frame_event_id": f"e{i}",
                "bus": "bindery",
                "frame_id": 10,
                "occurred_at_ns": arrival if arrival is not None else i,
                "payload_hex": payload.hex(),
            }

        events = [
            ev(0, 0, 1),                # pickup P1
            ev(1, 2, 2),                # pull P1 -> waste
            ev(2, 0, 3),                # pickup P2
            ev(3, 3, 4, book=1),        # seal (short)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "wh.db"
            with ObservationWarehouse(db) as wh:
                wh.ingest(product, events)
                service = BinderyService(wh)
                definition = service.publish_readout(
                    product,
                    {
                        "bus": "bindery", "frame_id": 10, "actions": ACTIONS,
                        "table_signal": "table", "machine_seq_signal": "seq",
                        "book_signal": "book", "selector_name": "act",
                    },
                    Path(tmp) / "readout.json",
                )
                result = service.reconstruct(
                    definition, PhysicalLedger([Load("1", 0, "P1"), Load("1", 1, "P2")])
                )
                self.assertEqual(result.books[0].product_tag, "P2")
                report = service.book_report(definition, result, result.books[0].book_id)
                self.assertEqual(report["sheet_tags"], ["P2"])
                self.assertEqual(report["product_sha256"], product.fingerprint)

                contract = Contract.parse({
                    "contract_id": "C", "publisher": "pub", "responsible_party": "party",
                    "period": "2026-11", "cap": 7,
                    "required": ["P1"], "approved": ["P1"],
                    "clauses": [{"clause": "1", "kind": "missing", "amount": 7}],
                })
                claims, allocations, targets = service.claim(result, [contract])
                self.assertEqual(targets, {"party|2026-11": {result.books[0].book_id: 7}})

                # The legacy observation query is still alive and version-bound.
                agg_v1 = wh.query_window(product, "speed", 0, 2**62)
                self.assertEqual(agg_v1.count, 4)
                self.assertGreater(agg_v1.total, 0)
                # Publishing a revision does not disturb the v1 physical answer.
                wh.ingest(revision, events)
                again = service.book_report(definition, result, result.books[0].book_id)
                self.assertEqual(again["product_sha256"], product.fingerprint)
                self.assertEqual(again["sheet_tags"], ["P2"])


if __name__ == "__main__":
    unittest.main()

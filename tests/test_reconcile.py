"""Day-over-day reconciliation snapshots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.bank import BankDouble  # noqa: E402
from bitcatalog.contracts import Contract  # noqa: E402
from bitcatalog.reconcile import Inventory, Snapshot, explain_difference  # noqa: E402
from bitcatalog.treasury import Treasury  # noqa: E402


ACCOUNT = "party|2026-11"


def make_contract():
    return Contract.parse({
        "contract_id": "C", "publisher": "pub", "responsible_party": "party",
        "period": "2026-11", "cap": 10,
        "required": ["P1"], "approved": ["P1"],
        "clauses": [{"clause": "1", "kind": "missing", "amount": 7}],
    })


def snapshot(label, bank, treasury, targets, inventory):
    return Snapshot.capture(
        label=label,
        taken_at=label,
        inventory=Inventory.of(inventory),
        contracts=[make_contract()],
        claims=[],
        targets={ACCOUNT: targets},
        instructions=bank.list_instructions(account_id=ACCOUNT),
        money={ACCOUNT: treasury.account_view(ACCOUNT, targets)},
    )


class ReconcileSnapshot(unittest.TestCase):
    def test_explains_target_and_spend_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = BankDouble(Path(tmp) / "b.db")
            treasury = Treasury(bank)
            # Yesterday: only B was known, paid 6.
            r = treasury.pay(account_id=ACCOUNT, book_id="B", amount=6)
            treasury.confirm(r["instruction_id"])
            before = snapshot("yesterday", bank, treasury, {"B": 6}, {"1": ["P1", "P2"]})

            # Today: late evidence raises targets to A=7, B=3.
            after = snapshot("today", bank, treasury, {"A": 7, "B": 3}, {"1": ["P1", "P3"]})
            diff = explain_difference(before, after)
            self.assertFalse(diff["reconciled"])
            self.assertEqual(diff["target_changes"][ACCOUNT]["A"], {"from": 0, "to": 7, "delta": 7})
            self.assertEqual(diff["target_changes"][ACCOUNT]["B"], {"from": 6, "to": 3, "delta": -3})
            self.assertEqual(diff["inventory_changes"],
                             [{"table": "1", "from": ["P1", "P2"], "to": ["P1", "P3"]}])
            # Explaining it again from the saved records gives the same answer.
            path = Path(tmp) / "snap.json"
            after.save(path)
            reloaded = Snapshot.load(path)
            self.assertEqual(explain_difference(before, reloaded), diff)
            bank.close()

    def test_new_instructions_and_state_changes_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = BankDouble(Path(tmp) / "b.db")
            treasury = Treasury(bank)
            targets = {"A": 7}
            before = snapshot("before", bank, treasury, targets, {"1": ["P1"]})
            decisions = treasury.reconcile(account_id=ACCOUNT, obligations=targets)
            treasury.confirm(decisions[0].instruction_id)
            after = snapshot("after", bank, treasury, targets, {"1": ["P1"]})
            diff = explain_difference(before, after)
            self.assertEqual(len(diff["new_instructions"]), 1)
            self.assertEqual(diff["new_instructions"][0]["amount"], 7)
            self.assertEqual(diff["money"][ACCOUNT]["paid_confirmed"]["delta"], 7)
            bank.close()


if __name__ == "__main__":
    unittest.main()

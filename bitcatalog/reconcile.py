"""Reconciliation snapshots: explain today's numbers from yesterday's records.

A snapshot retains, at one point in time:

* the *inventory* actually counted (which batch sits at which position on
  which material table),
* the contracts in force,
* the reliable physical claims and their cap allocation (``targets``),
* every instruction the cashier had already sent, with bank state,
* the cashier's money view (paid / refunded / pending).

:func:`explain_difference` then reports, per book and per account, exactly why
``target``, ``paid`` or ``safe_to_spend`` moved.  Because the inputs are kept,
the explanation can be regenerated later; nothing is reconstructed from
today's mutable state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import Contract


@dataclass(frozen=True)
class Inventory:
    """The counted table contents at snapshot time."""

    tables: Mapping[str, tuple[str, ...]]

    @classmethod
    def of(cls, tables: Mapping[str, Iterable[str]]) -> "Inventory":
        return cls({str(table): tuple(tags) for table, tags in tables.items()})

    def as_json(self) -> dict[str, list[str]]:
        return {table: list(tags) for table, tags in self.tables.items()}


@dataclass(frozen=True)
class Snapshot:
    label: str
    taken_at: str
    product_sha256: str | None
    readout_fingerprint: str | None
    inventory: Inventory
    contracts: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    targets: dict[str, dict[str, int]]
    instructions: tuple[dict[str, Any], ...]
    money: dict[str, dict[str, Any]]

    @classmethod
    def capture(
        cls,
        *,
        label: str,
        taken_at: str,
        inventory: Inventory,
        contracts: Iterable[Contract],
        claims: Iterable[Any],
        targets: Mapping[str, Mapping[str, int]],
        instructions: Iterable[Mapping[str, Any]],
        money: Mapping[str, Mapping[str, Any]],
        product_sha256: str | None = None,
        readout_fingerprint: str | None = None,
    ) -> "Snapshot":
        return cls(
            label=label,
            taken_at=taken_at,
            product_sha256=product_sha256,
            readout_fingerprint=readout_fingerprint,
            inventory=inventory,
            contracts=tuple(c.to_json() for c in contracts),
            claims=tuple(
                c.as_json() if hasattr(c, "as_json") else dict(c) for c in claims
            ),
            targets={account: dict(books) for account, books in targets.items()},
            instructions=tuple(
                {
                    "instruction_id": r["instruction_id"],
                    "kind": r["kind"],
                    "account_id": r["account_id"],
                    "book_id": r["book_id"],
                    "amount": r["amount"],
                    "state": r["state"],
                }
                for r in instructions
            ),
            money={account: dict(view) for account, view in money.items()},
        )

    def save(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_json(), indent=1, sort_keys=True,
                                     ensure_ascii=False) + "\n", encoding="utf-8")
        return str(target)

    @classmethod
    def load(cls, path: str | Path) -> "Snapshot":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            label=raw["label"],
            taken_at=raw["taken_at"],
            product_sha256=raw.get("product_sha256"),
            readout_fingerprint=raw.get("readout_fingerprint"),
            inventory=Inventory.of(raw["inventory"]),
            contracts=tuple(raw["contracts"]),
            claims=tuple(raw["claims"]),
            targets={a: dict(t) for a, t in raw["targets"].items()},
            instructions=tuple(raw["instructions"]),
            money={a: dict(v) for a, v in raw["money"].items()},
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "taken_at": self.taken_at,
            "product_sha256": self.product_sha256,
            "readout_fingerprint": self.readout_fingerprint,
            "inventory": self.inventory.as_json(),
            "contracts": list(self.contracts),
            "claims": list(self.claims),
            "targets": self.targets,
            "instructions": list(self.instructions),
            "money": self.money,
        }

    def instruction_ids(self) -> set[str]:
        return {i["instruction_id"] for i in self.instructions}

    def settled_payments(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.instructions:
            if row["kind"] == "payment" and row["state"] == "settled":
                out[row["book_id"]] = out.get(row["book_id"], 0) + row["amount"]
        return out


def explain_difference(previous: Snapshot, current: Snapshot) -> dict[str, Any]:
    """Why did targets, payments and safe-to-spend change between snapshots?"""

    targets: dict[str, Any] = {}
    accounts = set(previous.targets) | set(current.targets)
    for account in sorted(accounts):
        old = previous.targets.get(account, {})
        new = current.targets.get(account, {})
        books = set(old) | set(new)
        changes = {
            book: {"from": old.get(book, 0), "to": new.get(book, 0),
                   "delta": new.get(book, 0) - old.get(book, 0)}
            for book in sorted(books)
            if old.get(book, 0) != new.get(book, 0)
        }
        if changes:
            targets[account] = changes

    new_instructions = [
        i for i in current.instructions if i["instruction_id"] not in previous.instruction_ids()
    ]
    previous_instructions = {
        i["instruction_id"]: i for i in previous.instructions
    }
    state_changes = []
    for row in current.instructions:
        old = previous_instructions.get(row["instruction_id"])
        if old is not None and old["state"] != row["state"]:
            state_changes.append(
                {"instruction_id": row["instruction_id"], "from": old["state"], "to": row["state"]}
            )

    money: dict[str, Any] = {}
    for account in sorted(set(previous.money) | set(current.money)):
        old = previous.money.get(account, {})
        new = current.money.get(account, {})
        keys = ("target", "paid_confirmed", "refunded_confirmed", "pending_payment",
                "pending_refund", "safe_extra")
        diff = {}
        for key in keys:
            if old.get(key) != new.get(key):
                diff[key] = {"from": old.get(key), "to": new.get(key),
                             "delta": (new.get(key) or 0) - (old.get(key) or 0)}
        if diff:
            money[account] = diff

    inventory_changes = []
    tables = set(previous.inventory.tables) | set(current.inventory.tables)
    for table in sorted(tables):
        old = previous.inventory.tables.get(table, ())
        new = current.inventory.tables.get(table, ())
        if old != new:
            inventory_changes.append({"table": table, "from": list(old), "to": list(new)})

    return {
        "from": previous.label,
        "to": current.label,
        "target_changes": targets,
        "new_instructions": new_instructions,
        "state_changes": state_changes,
        "money": money,
        "inventory_changes": inventory_changes,
        "reconciled": (
            not targets
            and not new_instructions
            and not state_changes
            and not money
            and not inventory_changes
        ),
    }

"""High-level facade: publish readouts, reconstruct books, claim, reconcile.

This wires the layers together so an operator can go from stored raw frames to
a payable target and a bank instruction without re-implementing the glue::

    service = BinderyService(warehouse)
    definition = service.publish_readout(product, spec, "readout.json")
    result = service.reconstruct(definition, PhysicalLedger(loads))
    claims, allocations, targets = service.claim(result, contracts)

The original observation queries (``query_window`` and friends) stay on the
warehouse untouched; this facade only adds bindery-specific views.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .catalog import CatalogProduct
from .contracts import BookClaim, Contract, claims_and_allocations
from .physical import PhysicalLedger, PhysicalResult, query_book
from .readout import ReadoutDefinition, ReadoutFrame, decode_warehouse, publish_readout
from .reconcile import Inventory, Snapshot
from .treasury import Treasury


@dataclass
class BinderyService:
    warehouse: Any
    bank: Any | None = None

    # -- readouts / frames ----------------------------------------------

    def publish_readout(self, product: CatalogProduct, raw: Mapping[str, Any],
                        path: str) -> ReadoutDefinition:
        definition, _ = publish_readout(product, raw, path)
        return definition

    def frames(self, definition: ReadoutDefinition) -> list[ReadoutFrame]:
        return decode_warehouse(self.warehouse, definition)

    def original_frames(self, definition: ReadoutDefinition) -> list[ReadoutFrame]:
        """Alias that stresses these are the stored raw frames, not re-sent ones."""

        return self.frames(definition)

    def reconstruct(self, definition: ReadoutDefinition, ledger: PhysicalLedger) -> PhysicalResult:
        return ledger.reconstruct(self.frames(definition))

    def book_report(self, definition: ReadoutDefinition, result: PhysicalResult,
                    book_id: str) -> dict[str, Any]:
        return query_book(result, book_id, self.frames(definition))

    # -- claims / cash ---------------------------------------------------

    def claim(self, result: PhysicalResult, contracts: Iterable[Contract], **kwargs: Any):
        return claims_and_allocations(result, contracts, **kwargs)

    def treasury(self, fee_per_payment: int = 0) -> Treasury:
        if self.bank is None:
            raise ValueError("service has no bank")
        return Treasury(self.bank, fee_per_payment=fee_per_payment)

    # -- reconciliation --------------------------------------------------

    def snapshot(
        self,
        *,
        label: str,
        inventory: Mapping[str, Iterable[str]],
        contracts: Iterable[Contract],
        claims: Iterable[BookClaim],
        targets: Mapping[str, Mapping[str, int]],
        treasury: Treasury,
        accounts: Iterable[str],
        product_sha256: str | None = None,
        readout_fingerprint: str | None = None,
    ) -> Snapshot:
        accounts = list(accounts)
        all_instructions: list[dict[str, Any]] = []
        money: dict[str, dict[str, Any]] = {}
        for account in accounts:
            all_instructions.extend(treasury.bank.list_instructions(account_id=account))
            money[account] = treasury.account_view(account, targets.get(account, {}))
        return Snapshot.capture(
            label=label,
            taken_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            inventory=Inventory.of(inventory),
            contracts=contracts,
            claims=claims,
            targets=targets,
            instructions=all_instructions,
            money=money,
            product_sha256=product_sha256,
            readout_fingerprint=readout_fingerprint,
        )

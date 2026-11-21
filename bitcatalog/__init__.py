"""bitcatalog -- bit-field message catalog and bus observation warehouse.

Two source modules carry the substance:

* :mod:`bitcatalog.catalog` -- definition model, validation (including
  simultaneous-activity / physical-overlap proofs), bit masking, frame
  compilation, payload encoding and compiled-product serialisation.
* :mod:`bitcatalog.warehouse` -- SQLite observation warehouse that binds
  frames to an immutable catalog product and aggregates exact rationals.

:mod:`bitcatalog.reference` is an independent pure-Python bit-list model used
only by tests and cross-checks; it shares no bit arithmetic with the catalog.
"""

from .catalog import (
    CatalogError,
    CatalogProduct,
    CompileError,
    DecodeError,
    EncodedFrame,
    FrameCodec,
    MessageDef,
    SignalDef,
    SelectorDef,
    build_product,
    load_product,
)
from .warehouse import (
    FrameError,
    FrameEvent,
    FrameIdentity,
    IngestStats,
    ObservationWarehouse,
    SignalAggregate,
    StoredFrame,
    StoredProduct,
)
from .readout import (
    ACTIONS,
    ACTION_EMPTY_PUNCH,
    ACTION_PICKUP,
    ACTION_PULL_LAST,
    ACTION_SEAL,
    ReadoutCodec,
    ReadoutDefinition,
    ReadoutError,
    ReadoutFrame,
    load_readout,
    publish_readout,
)
from .physical import (
    ActionEvent,
    Book,
    Load,
    PhysicalLedger,
    PhysicalResult,
    explain_frame,
    physical_sheets_for_book,
)
from .contracts import (
    Allocation,
    BookClaim,
    Clause,
    Contract,
    allocate_caps,
    claims_and_allocations,
    evaluate_claims,
    target_obligations,
)
from .bank import (
    BankConflict,
    BankDisconnected,
    BankDouble,
    BankLedger,
    BankResult,
    Faults,
    Instruction,
    verify_instruction,
)
from .treasury import Decision, Treasury, TreasuryError, safe_spendable
from .reconcile import Inventory, Snapshot, explain_difference
from .service import BinderyService

__all__ = [
    "CatalogError",
    "CatalogProduct",
    "CompileError",
    "DecodeError",
    "EncodedFrame",
    "FrameCodec",
    "MessageDef",
    "SignalDef",
    "SelectorDef",
    "build_product",
    "load_product",
    "FrameError",
    "FrameEvent",
    "FrameIdentity",
    "IngestStats",
    "ObservationWarehouse",
    "SignalAggregate",
    "StoredFrame",
    "StoredProduct",
    "ACTIONS",
    "ACTION_EMPTY_PUNCH",
    "ACTION_PICKUP",
    "ACTION_PULL_LAST",
    "ACTION_SEAL",
    "ReadoutCodec",
    "ReadoutDefinition",
    "ReadoutError",
    "ReadoutFrame",
    "load_readout",
    "publish_readout",
    "ActionEvent",
    "Book",
    "Load",
    "PhysicalLedger",
    "PhysicalResult",
    "explain_frame",
    "physical_sheets_for_book",
    "Allocation",
    "BookClaim",
    "Clause",
    "Contract",
    "allocate_caps",
    "claims_and_allocations",
    "evaluate_claims",
    "target_obligations",
    "BankConflict",
    "BankDisconnected",
    "BankDouble",
    "BankLedger",
    "BankResult",
    "Faults",
    "Instruction",
    "verify_instruction",
    "Decision",
    "Treasury",
    "TreasuryError",
    "safe_spendable",
    "Inventory",
    "Snapshot",
    "explain_difference",
    "BinderyService",
]

__version__ = "0.2.0"

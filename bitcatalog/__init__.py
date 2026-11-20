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
]

__version__ = "0.1.0"

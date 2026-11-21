"""Physical binding state reconstructed from raw frames.

Frames arrive through a published :class:`~bitcatalog.readout.ReadoutDefinition`
and are grouped by *material table*.  A table is loaded with an ordered queue
of batches (``P1, P2, ...``); the table *cursor* points at the batch the next
real pickup would consume.

Actions inside one *book in progress* on a table
------------------------------------------------

``pickup``      real signature pickup: takes the batch at the cursor into the
                current book and advances the cursor by one.
``empty_punch`` no physical pick: the cursor does not move and nothing enters
                the book.
``pull_last``   removes the last sheet *inside the current book* into the
                waste basket.  The table cursor does not rewind.  Pulling from
                an empty book has no physical effect at all.
``seal``        seals the current book.  Its product is the last sheet still
                in the book (``sheet_tags[-1]``); the full physical sheet order
                is ``sheet_tags``.  Sealing does not touch the cursor.

The worked example ``load P1, P2; pickup, pull, pickup, seal`` therefore yields
P2: the first pickup puts P1 in the book, the pull wastes it, the second pickup
puts P2 in, and the seal takes P2.  If the first frame were actually an
``empty_punch`` the pull does nothing and the same later readings finish P1.

Late, duplicate and contradictory frames
----------------------------------------

Instants are ordered by ``machine_seq`` first (so a *late* frame with an
earlier sequence is replayed in its rightful position), then by machine,
occurrence time and event id.  In one ``(machine, machine_seq)`` slot:

* identical semantic readings are one duplicate -- applied once;
* differing readings are an *unresolved contradiction*.  Nothing in that slot
  applies and every frame there has status ``unclear`` until an operator calls
  :meth:`PhysicalLedger.resolve`.

An ``unclear`` frame never contributes to a payment (see
:mod:`bitcatalog.claims`).  Because discharge can change which batch a later
book receives, resolving a slot re-runs the entire history deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .readout import ReadoutFrame

CLEAR = "clear"
DUPLICATE = "duplicate"
UNCLEAR = "unclear"

# kinds reported for a clear, applied frame
APPLIED = "applied"
NOOP = "noop"
BLOCKED = "blocked"


@dataclass(frozen=True)
class Load:
    """A batch placed on a material table at queue position ``index``."""

    table: str
    index: int
    tag: str


@dataclass(frozen=True)
class Book:
    book_id: str
    machine: str
    book: int | None
    table: str
    sheet_tags: tuple[str, ...]
    sheet_frames: tuple[str, ...]
    product_tag: str | None
    sealed_at_ns: int
    seal_seq: int
    seal_frame_event_id: str
    short: bool
    product_sha256: str = ""

    def is_short(self) -> bool:
        return self.short


@dataclass(frozen=True)
class ActionEvent:
    machine: str
    machine_seq: int
    action: str | None
    table: int
    table_tag: str
    book: int | None
    occurred_at_ns: int
    frame_event_id: str
    status: str  # clear / duplicate / unclear
    kind: str  # applied / noop / blocked / duplicate / unclear
    batch: str | None = None
    pulled: tuple[str, ...] = ()
    sealed_book: str | None = None
    reason: str | None = None

    @property
    def clear(self) -> bool:
        return self.status == CLEAR

    @property
    def effective(self) -> bool:
        """True only for a clear frame that physically did something relevant."""

        return self.status == CLEAR and self.kind == APPLIED


@dataclass
class PhysicalResult:
    books: list[Book]
    events: list[ActionEvent]
    next_cursor: dict[str, int]
    waste: dict[str, list[str]]
    unclear_seqs: list[tuple[str, int, int]]
    tables: dict[str, list[str]] = field(default_factory=dict)

    def book(self, book_id: str) -> Book:
        for b in self.books:
            if b.book_id == book_id:
                return b
        raise KeyError(book_id)

    def event(self, frame_event_id: str) -> ActionEvent:
        for e in self.events:
            if e.frame_event_id == frame_event_id:
                return e
        raise KeyError(frame_event_id)

    def events_for(self, frame_event_id: str) -> list[ActionEvent]:
        return [e for e in self.events if e.frame_event_id == frame_event_id]

    def frame_is_clear(self, frame_event_id: str) -> bool:
        rows = self.events_for(frame_event_id)
        return bool(rows) and all(r.clear for r in rows)


class PhysicalLedger:
    """A replayable physical reconstruction over a set of loaded tables."""

    def __init__(self, loads: Iterable[Load] = (), *, resolutions: Mapping[tuple[str, int], str] | None = None):
        self._loads: dict[str, list[str]] = {}
        for load in loads:
            self.load(load.table, load.tag, index=load.index)
        # (machine, machine_seq) -> chosen frame_event_id, or "" for "keep unclear"
        self._resolutions: dict[tuple[str, int], str] = dict(resolutions or {})

    # -- loading / resolution ------------------------------------------

    def load(self, table: str, tag: str, *, index: int | None = None) -> None:
        queue = self._loads.setdefault(str(table), [])
        if tag in queue:
            raise ValueError(f"batch {tag!r} already loaded on table {table!r}")
        if index is None or index >= len(queue):
            queue.append(tag)
        else:
            if index < 0:
                raise ValueError("load index must be >= 0")
            queue.insert(index, tag)

    def tables(self) -> dict[str, list[str]]:
        return {table: list(queue) for table, queue in self._loads.items()}

    def resolve(self, machine: str, machine_seq: int, frame_event_id: str) -> None:
        """Pick the authoritative frame for a contradictory slot.

        Passing ``""`` keeps the slot permanently unclear.
        """

        self._resolutions[(machine, int(machine_seq))] = frame_event_id

    @property
    def resolutions(self) -> dict[tuple[str, int], str]:
        return dict(self._resolutions)

    # -- replay ---------------------------------------------------------

    def reconstruct(self, frames: Iterable[ReadoutFrame]) -> PhysicalResult:
        # Redelivery of one event id is a storage duplicate, not an action.
        unique: dict[str, ReadoutFrame] = {}
        for frame in frames:
            unique.setdefault(frame.frame_event_id, frame)

        slots: dict[tuple[str, int], list[ReadoutFrame]] = {}
        for frame in unique.values():
            slots.setdefault((frame.machine, frame.machine_seq), []).append(frame)

        cursor: dict[str, int] = {}
        # table -> list of (batch_tag, frame_event_id) currently in book
        current: dict[str, list[tuple[str, str]]] = {}
        # table -> True when the in-progress book lost a sheet to the waste bin
        short: dict[str, bool] = {}
        books: list[Book] = []
        events: list[ActionEvent] = []
        waste: dict[str, list[str]] = {}
        unclear_seqs: list[tuple[str, int]] = []

        order = sorted(slots, key=lambda key: (key[1], key[0]))
        for machine, seq in order:
            candidates = sorted(
                slots[(machine, seq)],
                key=lambda f: (f.occurred_at_ns, f.frame_event_id),
            )
            chosen, slot_status, slot_reason = self._resolve_slot(machine, seq, candidates)
            if slot_status == UNCLEAR:
                tables = sorted({frame.table for frame in candidates})
                for table in tables:
                    unclear_seqs.append((machine, seq, table))
            for frame in candidates:
                if slot_status == CLEAR and frame is chosen:
                    event = self._apply(frame, cursor, current, short, books, waste)
                    events.append(event)
                elif slot_status == CLEAR:
                    events.append(self._inert(frame, DUPLICATE, DUPLICATE, "duplicate of chosen frame"))
                else:
                    events.append(self._inert(frame, UNCLEAR, UNCLEAR, slot_reason))

        return PhysicalResult(
            books=sorted(books, key=lambda b: (b.seal_seq, b.machine, b.book_id)),
            events=events,
            next_cursor=dict(cursor),
            waste=waste,
            unclear_seqs=sorted(unclear_seqs),
            tables=self.tables(),
        )

    def _resolve_slot(
        self, machine: str, seq: int, candidates: list[ReadoutFrame]
    ) -> tuple[ReadoutFrame | None, str, str | None]:
        key = (machine, seq)
        if key in self._resolutions:
            chosen_id = self._resolutions[key]
            if chosen_id == "":
                return None, UNCLEAR, f"machine sequence {seq} marked unresolved"
            for frame in candidates:
                if frame.frame_event_id == chosen_id:
                    return frame, CLEAR, None
            return None, UNCLEAR, f"resolution names absent frame {chosen_id!r}"

        distinct = {frame.semantic_key for frame in candidates}
        if len(distinct) == 1:
            return candidates[0], CLEAR, None
        return None, UNCLEAR, f"contradictory readings at machine sequence {seq}"

    def _inert(self, frame: ReadoutFrame, status: str, kind: str, reason: str | None) -> ActionEvent:
        return ActionEvent(
            machine=frame.machine,
            machine_seq=frame.machine_seq,
            action=frame.action,
            table=frame.table,
            table_tag=str(frame.table),
            book=frame.book,
            occurred_at_ns=frame.occurred_at_ns,
            frame_event_id=frame.frame_event_id,
            status=status,
            kind=kind,
            reason=reason,
        )

    def _apply(
        self,
        frame: ReadoutFrame,
        cursor: dict[str, int],
        current: dict[str, list[tuple[str, str]]],
        short: dict[str, bool],
        books: list[Book],
        waste: dict[str, list[str]],
    ) -> ActionEvent:
        table_key = str(frame.table)
        queue = self._loads.get(table_key)
        position = cursor.get(table_key, 0)
        available = (queue is None) or position < len(queue)
        batch = (queue[position] if queue is not None and available else
                 (str(frame.table) if queue is None else None))

        kind = APPLIED
        pulled: tuple[str, ...] = ()
        sealed_book: str | None = None

        if frame.action == "empty_punch":
            kind = NOOP
        elif frame.action == "pickup":
            if not available or batch is None:
                kind = BLOCKED
            else:
                current.setdefault(table_key, []).append((batch, frame.frame_event_id))
                cursor[table_key] = position + 1
        elif frame.action == "pull_last":
            book = current.get(table_key, [])
            if not book:
                kind = NOOP
            else:
                removed_tag, _removed_frame = book.pop()
                waste.setdefault(table_key, []).append(removed_tag)
                pulled = (removed_tag,)
                short[table_key] = True
        elif frame.action == "seal":
            book = current.get(table_key, [])
            if not book:
                kind = BLOCKED
            else:
                identity = frame.book if frame.book is not None else frame.machine_seq
                sealed_book = f"{frame.machine}:{identity}"
                tags = tuple(tag for tag, _ in book)
                frames_in_book = tuple(fid for _, fid in book)
                books.append(
                    Book(
                        book_id=sealed_book,
                        machine=frame.machine,
                        book=frame.book,
                        table=table_key,
                        sheet_tags=tags,
                        sheet_frames=frames_in_book,
                        product_tag=tags[-1] if tags else None,
                        sealed_at_ns=frame.occurred_at_ns,
                        seal_seq=frame.machine_seq,
                        seal_frame_event_id=frame.frame_event_id,
                        short=short.get(table_key, False),
                        product_sha256=frame.product_sha256,
                    )
                )
                current[table_key] = []
                short[table_key] = False
        else:
            kind = NOOP

        return ActionEvent(
            machine=frame.machine,
            machine_seq=frame.machine_seq,
            action=frame.action,
            table=frame.table,
            table_tag=str(frame.table),
            book=frame.book,
            occurred_at_ns=frame.occurred_at_ns,
            frame_event_id=frame.frame_event_id,
            status=CLEAR,
            kind=kind,
            batch=batch if kind == APPLIED and frame.action == "pickup" else None,
            pulled=pulled,
            sealed_book=sealed_book,
            reason=None,
        )


def explain_frame(result: PhysicalResult, frame_event_id: str) -> dict[str, Any]:
    """What one frame did (or why it did not act)."""

    row = result.event(frame_event_id)
    return {
        "frame_event_id": frame_event_id,
        "machine": row.machine,
        "machine_seq": row.machine_seq,
        "action": row.action,
        "status": row.status,
        "kind": row.kind,
        "batch": row.batch,
        "pulled": list(row.pulled),
        "sealed_book": row.sealed_book,
        "reason": row.reason,
    }


def physical_sheets_for_book(result: PhysicalResult, book_id: str) -> dict[str, Any]:
    """The physical sheet order and contributing raw frames of one seal."""

    book = result.book(book_id)
    return {
        "book_id": book.book_id,
        "machine": book.machine,
        "book": book.book,
        "table": book.table,
        "product_tag": book.product_tag,
        "sheet_tags": list(book.sheet_tags),
        "sheet_frames": list(book.sheet_frames),
        "short": book.short,
        "seal_frame_event_id": book.seal_frame_event_id,
        "product_sha256": book.product_sha256,
    }


def query_book(
    result: PhysicalResult,
    book_id: str,
    frames: Iterable[ReadoutFrame],
) -> dict[str, Any]:
    """Answer "physical sheet order and original frames for this seal".

    The returned ``frames`` are the verbatim raw payloads of the seal and of
    every pickup that fed the book, all read under the product version the
    seal used (``product_sha256``).  Another product version never overwrites
    this answer.
    """

    report = physical_sheets_for_book(result, book_id)
    by_id = {frame.frame_event_id: frame for frame in frames}
    wanted = list(report["sheet_frames"]) + [report["seal_frame_event_id"]]
    report["frames"] = [
        by_id[fid].as_json() for fid in wanted if fid in by_id
    ]
    return report

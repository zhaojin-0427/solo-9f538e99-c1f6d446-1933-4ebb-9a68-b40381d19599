"""Domain logic: revision arbitration, atomic batch apply, plan reconciliation.

All write paths run inside one ``BEGIN IMMEDIATE`` transaction.  Any rule
violation raises ``DomainConflict`` / ``DomainValidation`` and rolls the whole
batch back, so the previously committed chain and plan are preserved untouched.
Read paths use a single WAL read snapshot so plan, scans and chain always come
from the same committed state.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .schemas import (
    BatchResponse,
    BatchSubmit,
    BreakReason,
    ChainResponse,
    MatchSummary,
    PlanItemStatus,
    PlanItemView,
    PlanReplace,
    PlanReplaceResponse,
    PlanView,
    Reconciliation,
    ScanEntry,
    ScanResolution,
    ScanStatus,
    ScanView,
    WaybillCreate,
    entry_payload_hash,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Plan item windows are stored in the same fixed-width UTC form as scan times.
_PLAN_TS_FMT = _STORAGE_FMT


@dataclass(frozen=True)
class _PlanItem:
    index: int  # 0-based position in plan order
    receiver: str
    start: datetime
    end: datetime
    required: bool


@dataclass(frozen=True)
class _StoredPlan:
    revision: int
    items: tuple[_PlanItem, ...]


class DomainConflict(Exception):
    """HTTP 409: same-revision divergence, stale revision, unknown scan ..."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


# --------------------------------------------------------------------------- #
# Waybills
# --------------------------------------------------------------------------- #
def create_waybill(conn: sqlite3.Connection, body: WaybillCreate) -> dict:
    from .database import transaction

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM waybills WHERE id = ?", (body.waybill_id,)
        ).fetchone()
        if existing is not None:
            raise DomainConflict(f"waybill {body.waybill_id!r} already exists")
        conn.execute(
            "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
            (body.waybill_id, body.origin_party, utcnow_iso()),
        )
    return {"waybill_id": body.waybill_id, "origin_party": body.origin_party}


def get_waybill(conn: sqlite3.Connection, waybill_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    return row


# --------------------------------------------------------------------------- #
# Batch submission (handover reports, corrections, withdrawals)
# --------------------------------------------------------------------------- #
def apply_batch(
    conn: sqlite3.Connection, waybill_id: str, batch: BatchSubmit
) -> BatchResponse:
    from .database import transaction

    get_waybill(conn, waybill_id)  # 404-style check before opening the txn

    applied = 0
    idempotent = 0
    withdrawn: list[str] = []

    with transaction(conn):
        for entry in batch.scans:
            wrote = _apply_one(conn, waybill_id, entry)
            if not wrote:
                idempotent += 1
                continue
            if entry.event_type == "WITHDRAWAL":
                withdrawn.append(entry.scan_id)
            applied += 1

        # Computed inside the same write transaction, so the receipt reflects
        # exactly the state this submission committed -- never a later write
        # that happens to slip in between COMMIT and the response.
        chain = _compute_chain_locked(conn, waybill_id)
        plan = load_plan(conn, waybill_id)
        reconciliation = _reconcile_locked(conn, waybill_id, chain=chain, plan=plan)

    chain.plan_revision = plan.revision if plan else None
    chain.reconciliation = reconciliation

    return BatchResponse(
        waybill_id=waybill_id,
        applied=applied,
        idempotent_duplicates=idempotent,
        withdrawn_scan_ids=withdrawn,
        plan_revision=chain.plan_revision,
        reconciliation=reconciliation,
        chain=chain,
    )


def _apply_one(conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry) -> bool:
    """Insert one scan revision.  Returns False when it was an idempotent no-op."""
    h = entry_payload_hash(entry)

    newest = conn.execute(
        "SELECT revision, event_type, payload_hash "
        "FROM scans WHERE waybill_id = ? AND scan_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (waybill_id, entry.scan_id),
    ).fetchone()

    if newest is not None and newest["revision"] > entry.revision:
        raise DomainConflict(
            f"scan_id={entry.scan_id!r} revision={entry.revision} is stale: "
            f"revision {newest['revision']} already adopted"
        )

    same_rev = conn.execute(
        "SELECT event_type, payload_hash FROM scans "
        "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
        (waybill_id, entry.scan_id, entry.revision),
    ).fetchone()

    if same_rev is not None:
        # 同修订异文 -> 整批拒绝；同文 -> 幂等跳过
        if same_rev["payload_hash"] != h:
            raise DomainConflict(
                f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                "exists with different content (same-revision divergence)"
            )
        return False

    if entry.event_type == "WITHDRAWAL" and newest is None:
        raise DomainConflict(
            f"cannot withdraw unknown scan_id={entry.scan_id!r}: no prior revision exists"
        )

    ts_text = to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None
    conn.execute(
        "INSERT INTO scans (waybill_id, scan_id, revision, event_type, "
        "ts_utc, giver, receiver, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            waybill_id,
            entry.scan_id,
            entry.revision,
            entry.event_type,
            ts_text,
            entry.giver,
            entry.receiver,
            h,
            utcnow_iso(),
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Handover plan registration (full replacement per waybill)
# --------------------------------------------------------------------------- #
def _normalize_plan_items(body: PlanReplace) -> list[dict]:
    items: list[dict] = []
    for item in body.items:
        items.append(
            {
                "receiver": item.receiver,
                "window_start": to_utc_iso(item.window_start),
                "window_end": to_utc_iso(item.window_end),
                "required": item.required,
            }
        )
    return items


def replace_plan(
    conn: sqlite3.Connection, waybill_id: str, body: PlanReplace
) -> PlanReplaceResponse:
    """Register or fully replace the waybill's handover plan atomically.

    Every call swaps the whole item list, so a replacement never merges with
    the previous plan.  ``revision`` is the version identifier this submission
    corresponds to; when omitted it becomes current + 1 (1 on first
    registration).  The response carries that plan revision and the
    reconciliation of the state this call committed.
    """
    from .database import transaction

    get_waybill(conn, waybill_id)
    items = _normalize_plan_items(body)

    with transaction(conn):
        current = conn.execute(
            "SELECT revision FROM custody_plans WHERE waybill_id = ?", (waybill_id,)
        ).fetchone()
        adopted = current["revision"] if current is not None else 0
        revision = body.revision if body.revision is not None else adopted + 1
        conn.execute(
            "INSERT INTO custody_plans (waybill_id, revision, items_json, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(waybill_id) DO UPDATE SET "
            "revision = excluded.revision, items_json = excluded.items_json, "
            "created_at = excluded.created_at",
            (waybill_id, revision, json.dumps(items, sort_keys=True), utcnow_iso()),
        )
        # Read back inside the same write transaction: the receipt describes
        # exactly the plan version and scan state this call committed.
        plan = load_plan(conn, waybill_id)
        chain = _compute_chain_locked(conn, waybill_id)
        reconciliation = _reconcile_locked(conn, waybill_id, chain=chain, plan=plan)

    return PlanReplaceResponse(
        waybill_id=waybill_id,
        plan_revision=plan.revision if plan else None,
        plan=_plan_view(waybill_id, plan),
        reconciliation=reconciliation,
    )


def load_plan(conn: sqlite3.Connection, waybill_id: str) -> "_StoredPlan | None":
    row = conn.execute(
        "SELECT revision, items_json FROM custody_plans WHERE waybill_id = ?",
        (waybill_id,),
    ).fetchone()
    if row is None:
        return None
    raw = json.loads(row["items_json"])
    items = tuple(
        _PlanItem(
            index=i,
            receiver=entry["receiver"],
            start=datetime.strptime(entry["window_start"], _PLAN_TS_FMT).replace(
                tzinfo=timezone.utc
            ),
            end=datetime.strptime(entry["window_end"], _PLAN_TS_FMT).replace(
                tzinfo=timezone.utc
            ),
            required=bool(entry["required"]),
        )
        for i, entry in enumerate(raw)
    )
    return _StoredPlan(revision=row["revision"], items=items)


def _plan_view(waybill_id: str, plan: "_StoredPlan | None") -> PlanView | None:
    if plan is None:
        return None
    return PlanView(
        waybill_id=waybill_id,
        revision=plan.revision,
        items=[
            PlanItemView(
                index=item.index,
                receiver=item.receiver,
                window_start=item.start,
                window_end=item.end,
                required=item.required,
            )
            for item in plan.items
        ],
    )


def get_plan(conn: sqlite3.Connection, waybill_id: str) -> PlanView | None:
    """Read the currently registered plan (None when the waybill has none)."""
    get_waybill(conn, waybill_id)
    return _plan_view(waybill_id, load_plan(conn, waybill_id))


# --------------------------------------------------------------------------- #
# Chain recomputation
# --------------------------------------------------------------------------- #
def _adopted_handovers(
    conn: sqlite3.Connection, waybill_id: str
) -> list[sqlite3.Row]:
    """Return handover rows for each scan's highest revision, time-ordered.

    Scans whose highest revision is a WITHDRAWAL tombstone do not appear.
    """
    return conn.execute(
        """
        SELECT s.scan_id, s.revision, s.event_type, s.ts_utc, s.giver, s.receiver
        FROM scans AS s
        JOIN (
            SELECT scan_id, MAX(revision) AS max_revision
            FROM scans
            WHERE waybill_id = ?
            GROUP BY scan_id
        ) AS m
          ON m.scan_id = s.scan_id AND m.max_revision = s.revision
        WHERE s.waybill_id = ? AND s.event_type = 'HANDOVER'
        ORDER BY s.ts_utc ASC, s.scan_id ASC
        """,
        (waybill_id, waybill_id),
    ).fetchall()


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


def compute_chain(conn: sqlite3.Connection, waybill_id: str) -> ChainResponse:
    """Recompute the verified prefix from scans and the registered plan."""
    from .database import read_transaction

    with read_transaction(conn):
        chain = _compute_chain_locked(conn, waybill_id)
        plan = load_plan(conn, waybill_id)
        reconciliation = _reconcile_locked(conn, waybill_id, chain=chain, plan=plan)
    chain.plan_revision = plan.revision if plan else None
    chain.reconciliation = reconciliation
    return chain


def _compute_chain_locked(
    conn: sqlite3.Connection, waybill_id: str
) -> ChainResponse:
    waybill = get_waybill(conn, waybill_id)
    origin = waybill["origin_party"]

    rows = _adopted_handovers(conn, waybill_id)

    # Group by timestamp: simultaneous handovers cannot be ordered by guessing.
    groups: list[tuple[str, list[sqlite3.Row]]] = []
    for row in rows:
        if groups and groups[-1][0] == row["ts_utc"]:
            groups[-1][1].append(row)
        else:
            groups.append((row["ts_utc"], [row]))

    verified: list[ScanView] = []
    unverified: list[ScanView] = []
    custodian = origin
    broken = False
    break_reason = BreakReason.none
    break_detail: str | None = None
    break_scan_ids: list[str] = []

    def view(row: sqlite3.Row, ok: bool, reason: BreakReason | None) -> ScanView:
        return ScanView(
            scan_id=row["scan_id"],
            revision=row["revision"],
            event_type=row["event_type"],
            ts_utc=_parse_ts(row["ts_utc"]),
            giver=row["giver"],
            receiver=row["receiver"],
            verified=ok,
            unverified_reason=reason,
        )

    for ts, group in groups:
        if broken:
            # 断链之后的扫描一律未证实，直到补报/更正/撤回补齐缺口
            unverified.extend(view(r, False, break_reason) for r in group)
            continue

        if len(group) > 1:
            broken = True
            break_reason = BreakReason.simultaneous
            break_scan_ids = [r["scan_id"] for r in group]
            break_detail = (
                f"{len(group)} handovers share ts_utc={ts}; order cannot be "
                "determined, so none of them is verified"
            )
            unverified.extend(view(r, False, BreakReason.simultaneous) for r in group)
            continue

        scan = group[0]
        if scan["giver"] != custodian:
            broken = True
            break_reason = BreakReason.gap
            break_scan_ids = [scan["scan_id"]]
            break_detail = (
                f"giver {scan['giver']!r} does not hold custody; expected "
                f"{custodian!r} (previous receiver / origin party)"
            )
            unverified.append(view(scan, False, BreakReason.gap))
            continue

        verified.append(view(scan, True, None))
        custodian = scan["receiver"]

    return ChainResponse(
        waybill_id=waybill_id,
        origin_party=origin,
        current_custodian=custodian,
        verified_prefix=verified,
        unverified_scans=unverified,
        chain_intact=not broken,
        break_reason=break_reason,
        break_detail=break_detail,
        break_scan_ids=break_scan_ids,
    )


# --------------------------------------------------------------------------- #
# Reconciliation: legal correspondences between the trusted scan prefix and
# the registered handover plan
# --------------------------------------------------------------------------- #
def _compatible(scan: ScanView, item: _PlanItem) -> bool:
    """Receiver matches and the scan time falls inside the inclusive window."""
    if scan.receiver != item.receiver:
        return False
    assert scan.ts_utc is not None  # HANDOVER scans always carry a timestamp
    ts = scan.ts_utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return item.start <= ts <= item.end


def _correspondence_table(
    scans: list[ScanView], items: tuple[_PlanItem, ...]
) -> tuple[list[list[int]], int] | None:
    """Forward DP over (scan index, next plan item index).

    A prefix of ``scans`` corresponds legally to the plan when every scan is
    assigned a distinct, increasing plan item whose receiver and time window
    match, while between assigned items (and before the first one) only
    non-required items may be skipped.  Items left after the last matched item
    are simply left for later scans, required or not.

    Returns ``(forward, total)`` where ``forward[i][j]`` counts the paths
    reaching state (first ``i`` scans matched, next item ``j``), and ``total``
    counts distinct scan -> item assignments (a trailing run of skipped items
    is not a different correspondence).  ``None`` when no legal
    correspondence exists.
    """
    n = len(scans)
    m = len(items)
    if n > m:
        return None

    compat = [[_compatible(scans[i], items[j]) for j in range(m)] for i in range(n)]

    forward = [[0] * (m + 1) for _ in range(n + 1)]
    forward[0][0] = 1
    for i in range(n + 1):
        for j in range(m + 1):
            here = forward[i][j]
            if not here or i == n:
                continue
            if j < m:
                if compat[i][j]:
                    forward[i + 1][j + 1] += here
                if not items[j].required:
                    # skip a non-required item before the first / between scans
                    forward[i][j + 1] += here

    if n == 0:
        total = 1  # the empty prefix trivially corresponds to the plan
    else:
        # Finalise the moment the last scan is matched, so that differently
        # long trailing skips do not inflate the count.
        total = sum(
            forward[n - 1][j] for j in range(m) if compat[n - 1][j]
        )
    if total == 0:
        return None
    return forward, total


def _suffix_counts(
    scans: list[ScanView], items: tuple[_PlanItem, ...]
) -> list[list[int]]:
    """``suffix[i][j]`` = ways to finish matching all remaining scans from state
    (first ``i`` scans matched, item ``j`` next)."""
    n = len(scans)
    m = len(items)
    compat = [[_compatible(scans[i], items[j]) for j in range(m)] for i in range(n)]
    suffix = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        suffix[n][j] = 1  # every remaining plan item is simply left for later
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            ways = 0
            if compat[i][j]:
                ways += suffix[i + 1][j + 1]
            if not items[j].required:
                ways += suffix[i][j + 1]
            suffix[i][j] = ways
    return suffix


def _match_status(
    scans: list[ScanView],
    items: tuple[_PlanItem, ...],
    forward: list[list[int]],
    total: int,
) -> tuple[list[PlanItemStatus], list[ScanStatus]]:
    suffix = _suffix_counts(scans, items)
    n = len(scans)
    m = len(items)
    compat = [[_compatible(scans[i], items[j]) for j in range(m)] for i in range(n)]

    # How many correspondences pass through each (scan i -> item j) edge.
    edge = [[0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            if compat[i][j] and forward[i][j]:
                edge[i][j] = forward[i][j] * suffix[i + 1][j + 1]

    item_statuses: list[PlanItemStatus] = []
    for j, item in enumerate(items):
        supporting = [scans[i].scan_id for i in range(n) if edge[i][j] > 0]
        matched = sum(edge[i][j] for i in range(n))
        if matched == total:
            status = "CONFIRMED"
        elif matched > 0:
            status = "POSSIBLE"
        else:
            status = "UNSUPPORTED"
        item_statuses.append(
            PlanItemStatus(
                index=item.index,
                receiver=item.receiver,
                required=item.required,
                status=status,
                supporting_scan_ids=supporting,
            )
        )

    scan_statuses: list[ScanStatus] = []
    for i, scan in enumerate(scans):
        possible = [j for j in range(m) if edge[i][j] > 0]
        status = "CONFIRMED" if len(possible) == 1 else "POSSIBLE"
        scan_statuses.append(
            ScanStatus(
                scan_id=scan.scan_id,
                revision=scan.revision,
                status=status,
                plan_indices=possible,
            )
        )
    return item_statuses, scan_statuses


def _reconcile_locked(
    conn: sqlite3.Connection,
    waybill_id: str,
    *,
    chain: ChainResponse,
    plan: "_StoredPlan | None",
) -> Reconciliation:
    scans = chain.verified_prefix  # trusted prefix, in order
    base = dict(
        waybill_id=waybill_id,
        plan_revision=plan.revision if plan else None,
        has_plan=plan is not None,
        scans_considered=len(scans),
        chain_intact=chain.chain_intact,
    )
    if plan is None:
        return Reconciliation(
            **base,
            match=MatchSummary(),
            items=[],
            scans=[],
            unresolved=None,
            required_covered=False,
            complete=False,
        )

    items = plan.items
    if not scans:
        return Reconciliation(
            **base,
            match=MatchSummary(),
            items=[
                PlanItemStatus(
                    index=item.index,
                    receiver=item.receiver,
                    required=item.required,
                    status="UNSUPPORTED",
                    supporting_scan_ids=[],
                )
                for item in items
            ],
            scans=[],
            unresolved=None,
            required_covered=False,
            complete=False,
        )

    result = _correspondence_table(scans, items)

    if result is None:
        # Earliest prefix that is no longer matchable.  Matchability is
        # monotone, so the first failing prefix pinpoints the offending scan.
        failing_index = 0
        for k in range(1, len(scans) + 1):
            if _correspondence_table(scans[:k], items) is None:
                failing_index = k - 1
                break
        scan = scans[failing_index]
        before = failing_index
        unresolved = ScanResolution(
            scan_id=scan.scan_id,
            revision=scan.revision,
            prefix_length=failing_index + 1,
            detail=(
                f"trusted scan prefix of {failing_index + 1} scan(s) has no legal "
                f"correspondence to plan revision {plan.revision}; receiver "
                f"{scan.receiver!r} at {scan.ts_utc.isoformat() if scan.ts_utc else '?'}"
                f" cannot be placed after {before} earlier scan(s) under the "
                "skip / window rules"
            ),
        )
        return Reconciliation(
            **base,
            match=MatchSummary(),
            items=[],
            scans=[],
            unresolved=unresolved,
            required_covered=False,
            complete=False,
        )

    forward, total = result
    item_statuses, scan_statuses = _match_status(scans, items, forward, total)
    required_covered = all(
        s.status == "CONFIRMED" for s in item_statuses if s.required
    )
    return Reconciliation(
        **base,
        match=MatchSummary(correspondence_count=total, max_scans=len(scans)),
        items=item_statuses,
        scans=scan_statuses,
        unresolved=None,
        required_covered=required_covered,
        complete=chain.chain_intact and required_covered,
    )


def reconcile(
    conn: sqlite3.Connection, waybill_id: str
) -> Reconciliation:
    """Plan + scans + chain read from one committed snapshot."""
    from .database import read_transaction

    with read_transaction(conn):
        chain = _compute_chain_locked(conn, waybill_id)
        plan = load_plan(conn, waybill_id)
        return _reconcile_locked(conn, waybill_id, chain=chain, plan=plan)


# Re-export so other modules can reuse the domain exceptions.
__all__ = [
    "DomainConflict",
    "DomainValidation",
    "apply_batch",
    "compute_chain",
    "create_waybill",
    "get_plan",
    "get_waybill",
    "reconcile",
    "replace_plan",
]

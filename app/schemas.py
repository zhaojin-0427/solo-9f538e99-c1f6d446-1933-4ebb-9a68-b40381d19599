"""Pydantic request/response models and payload canonicalization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class WaybillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waybill_id: str = Field(min_length=1, description="运单号，全局唯一")
    origin_party: str = Field(min_length=1, description="固定起始保管方")

    @field_validator("waybill_id", "origin_party")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class ScanEntry(BaseModel):
    """One scan inside a batch submission.

    ``event_type`` defaults to HANDOVER so the common case only carries
    scan_id / ts_utc / giver / receiver / revision.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    revision: int = Field(gt=0, description="正整数修订号；同 ID 仅最高修订生效")
    event_type: Literal["HANDOVER", "WITHDRAWAL"] = "HANDOVER"
    ts_utc: datetime | None = Field(
        default=None, description="ISO-8601，必须带时区偏移（推荐 UTC，如 2026-11-18T08:30:00Z）"
    )
    giver: str | None = None
    receiver: str | None = None

    @field_validator("scan_id")
    @classmethod
    def _strip_scan_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("giver", "receiver")
    @classmethod
    def _strip_parties(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("ts_utc")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("ts_utc must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "ScanEntry":
        if self.event_type == "HANDOVER":
            missing = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"HANDOVER entry requires fields: {', '.join(missing)}")
        else:  # WITHDRAWAL: only id + higher revision are accepted
            extras = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is not None
            ]
            if extras:
                raise ValueError(
                    f"WITHDRAWAL entry must only carry scan_id and revision; got extra: {', '.join(extras)}"
                )
        return self


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[ScanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch_duplicates(self) -> "BatchSubmit":
        # Same revision appearing twice in one batch with different text is a
        # whole-batch rejection; identical text is treated as idempotent.
        seen: dict[tuple[str, int], str] = {}
        for entry in self.scans:
            key = (entry.scan_id, entry.revision)
            h = entry_payload_hash(entry)
            if key in seen and seen[key] != h:
                raise ValueError(
                    f"scan_id={key[0]} revision={key[1]} appears multiple times "
                    "with conflicting content in the same batch"
                )
            seen[key] = h
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class BreakReason(str, Enum):
    none = "NONE"
    gap = "GAP"  # 保管方不承接：giver != 前一 receiver
    simultaneous = "SIMULTANEOUS"  # 同一时刻多笔交接，顺序不可判定


class ScanView(BaseModel):
    scan_id: str
    revision: int
    event_type: Literal["HANDOVER", "WITHDRAWAL"]
    ts_utc: datetime | None
    giver: str | None
    receiver: str | None
    verified: bool
    unverified_reason: BreakReason | None = None


class ChainResponse(BaseModel):
    waybill_id: str
    origin_party: str
    current_custodian: str
    verified_prefix: list[ScanView]
    unverified_scans: list[ScanView]
    chain_intact: bool
    break_reason: BreakReason
    break_detail: str | None = None
    break_scan_ids: list[str] = Field(default_factory=list)
    # Plan version and reconciliation read from the same committed snapshot.
    plan_revision: int | None = None
    reconciliation: "Reconciliation | None" = None


# --------------------------------------------------------------------------- #
# Handover plan (registered expectations) and reconciliation
# --------------------------------------------------------------------------- #
class PlanItemIn(BaseModel):
    """One planned handover: receiver, inclusive UTC window, mandatory flag."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    receiver: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "receiver", "expected_receiver", "custodian", "party"
        ),
        description="计划接收方，可重复出现",
    )
    window_start: datetime = Field(
        validation_alias=AliasChoices(
            "window_start", "start", "start_utc", "from_utc", "window_from"
        ),
        description="含首 UTC 时间窗起点",
    )
    window_end: datetime = Field(
        validation_alias=AliasChoices(
            "window_end", "end", "end_utc", "to_utc", "window_to"
        ),
        description="含尾 UTC 时间窗终点",
    )
    required: bool = Field(
        default=False,
        validation_alias=AliasChoices("required", "mandatory", "is_required"),
        description="是否必经项",
    )

    @model_validator(mode="before")
    @classmethod
    def _flatten_window(cls, data: object) -> object:
        """Accept ``{"receiver": ..., "window": {"start": ..., "end": ...}}``."""
        if isinstance(data, dict) and isinstance(data.get("window"), dict):
            win = data["window"]
            data = {k: v for k, v in data.items() if k != "window"}
            data.setdefault("window_start", win.get("start", win.get("from")))
            data.setdefault("window_end", win.get("end", win.get("to")))
        return data

    @field_validator("receiver")
    @classmethod
    def _strip_receiver(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("window_start", "window_end")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("window bounds must carry an explicit timezone offset")
        return v

    @model_validator(mode="after")
    def _ordered_window(self) -> "PlanItemIn":
        if self.window_start > self.window_end:
            raise ValueError("window_start must not be after window_end")
        return self


class PlanReplace(BaseModel):
    """Full replacement of a waybill's registered handover plan."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    revision: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("revision", "plan_revision", "version"),
        description="计划版号；省略时自动取当前版号 + 1",
    )
    items: list[PlanItemIn] = Field(
        min_length=1,
        max_length=60,
        validation_alias=AliasChoices("items", "plan", "entries", "steps"),
    )


class PlanItemView(BaseModel):
    index: int
    receiver: str
    window_start: datetime
    window_end: datetime
    required: bool


class PlanView(BaseModel):
    waybill_id: str
    revision: int | None
    items: list[PlanItemView]


class PlanReplaceResponse(BaseModel):
    waybill_id: str
    plan_revision: int | None
    plan: PlanView | None
    reconciliation: "Reconciliation"


class MatchSummary(BaseModel):
    """Aggregate of all legal correspondences (never enumerated: it can explode)."""

    correspondence_count: int = 0
    max_scans: int = 0


class PlanItemStatus(BaseModel):
    index: int
    receiver: str
    required: bool
    status: Literal["CONFIRMED", "POSSIBLE", "UNSUPPORTED"]
    supporting_scan_ids: list[str]


class ScanStatus(BaseModel):
    scan_id: str
    revision: int
    status: Literal["CONFIRMED", "POSSIBLE"]
    plan_indices: list[int]


class ScanResolution(BaseModel):
    """Earliest scan revision whose prefix can no longer be matched."""

    scan_id: str
    revision: int
    prefix_length: int
    detail: str


class Reconciliation(BaseModel):
    waybill_id: str
    plan_revision: int | None
    has_plan: bool
    scans_considered: int
    match: MatchSummary
    items: list[PlanItemStatus]
    scans: list[ScanStatus]
    unresolved: ScanResolution | None = None
    chain_intact: bool
    required_covered: bool
    complete: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def earliest_unmatched_scan_id(self) -> str | None:
        """Earliest scan that makes the prefix unmatchable (None if matched)."""
        return self.unresolved.scan_id if self.unresolved else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def earliest_unmatched_revision(self) -> int | None:
        return self.unresolved.revision if self.unresolved else None


class BatchResponse(BaseModel):
    waybill_id: str
    applied: int
    idempotent_duplicates: int
    withdrawn_scan_ids: list[str]
    # Plan version and reconciliation at the moment this submission committed.
    plan_revision: int | None = None
    reconciliation: Reconciliation | None = None
    chain: ChainResponse


PlanReplaceResponse.model_rebuild()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Normalize any timezone-aware datetime to a fixed-width UTC ISO string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_payload(entry: ScanEntry) -> dict:
    """Deterministic representation used to detect same-revision divergence."""
    if entry.event_type == "WITHDRAWAL":
        return {"event_type": "WITHDRAWAL"}
    return {
        "event_type": "HANDOVER",
        "ts_utc": to_utc_iso(entry.ts_utc),  # type: ignore[arg-type]
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def payload_hash_of(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def entry_payload_hash(entry: ScanEntry) -> str:
    return payload_hash_of(canonical_payload(entry))

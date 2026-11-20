"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

# --- handover plan registration + reconciliation --------------------------- #
# Remove the deliberately-ambiguous S4/S5 pair so the trusted prefix is clean:
# S1 (08:00 ORIG->B), S2 (09:00 B->C), S3 rev3 (10:00 C->D).
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 2, "event_type": "WITHDRAWAL"},
    {"scan_id": "S5", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("simultaneous pair withdrawn", st == 200 and body["chain"]["chain_intact"] is True, body)
st, body = call("PUT", "/api/waybills/AWB-SMOKE/plan", {"revision": 1, "items": [
    {"receiver": "B", "window_start": "2026-11-18T07:00:00Z",
     "window_end": "2026-11-18T09:00:00Z", "required": True},
    {"receiver": "C", "window_start": "2026-11-18T09:00:00Z",
     "window_end": "2026-11-18T09:30:00Z", "required": True},
    {"receiver": "D", "window_start": "2026-11-18T09:30:00Z",
     "window_end": "2026-11-18T10:00:00Z", "required": True},
]})
check("plan registered", st == 200 and body["plan_revision"] == 1, body)
check("plan receipt carries reconciliation",
      body["reconciliation"]["plan_revision"] == 1, body["reconciliation"])

st, body = call("GET", "/api/waybills/AWB-SMOKE/reconciliation")
check("reconciliation query", st == 200 and body["plan_revision"] == 1, body)
check("all required items confirmed",
      [i["status"] for i in body["items"]] == ["CONFIRMED"] * 3, body["items"])
check("journey complete", body["complete"] is True, body)

# A trusted scan that fits no plan item -> earliest unmatchable prefix.
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S6", "revision": 1, "ts_utc": "2026-11-18T10:20:00Z",
     "giver": "D", "receiver": "Z"},
]})
check("batch receipt carries plan revision", st == 200 and body["plan_revision"] == 1, st)
check("unmatchable scan located",
      body["reconciliation"]["unresolved"]["scan_id"] == "S6"
      and body["reconciliation"]["unresolved"]["revision"] == 1,
      body["reconciliation"])

# correction makes the prefix matchable again
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S6", "revision": 2, "ts_utc": "2026-11-18T10:20:00Z",
     "giver": "D", "receiver": "Z"},
]})
st, body = call("PUT", "/api/waybills/AWB-SMOKE/plan", {"revision": 2, "items": [
    {"receiver": "B", "window_start": "2026-11-18T07:00:00Z",
     "window_end": "2026-11-18T09:00:00Z", "required": True},
    {"receiver": "C", "window_start": "2026-11-18T09:00:00Z",
     "window_end": "2026-11-18T09:30:00Z", "required": True},
    {"receiver": "D", "window_start": "2026-11-18T09:30:00Z",
     "window_end": "2026-11-18T10:00:00Z", "required": True},
    {"receiver": "Z", "window_start": "2026-11-18T10:00:00Z",
     "window_end": "2026-11-18T11:00:00Z", "required": True},
]})
check("plan replacement reconciled", st == 200 and body["plan_revision"] == 2, st)
check("replacement receipt confirms every item",
      [i["status"] for i in body["reconciliation"]["items"]] == ["CONFIRMED"] * 4,
      body["reconciliation"])
check("replacement receipt completes journey",
      body["reconciliation"]["complete"] is True, body["reconciliation"])

st, body = call("PUT", "/api/waybills/AWB-SMOKE/plan", {"revision": 3, "items": [
    {"receiver": "NOPE", "window_start": "2026-11-18T07:00:00Z",
     "window_end": "2026-11-18T08:00:00Z", "required": True},
]})
check("full plan replacement accepted", st == 200 and body["plan_revision"] == 3, body)
check("replacement swaps the whole plan",
      [i["receiver"] for i in body["plan"]["items"]] == ["NOPE"], body["plan"])
check("remaining scan unmatched after replacement",
      body["reconciliation"]["unresolved"] is not None, body["reconciliation"])
st, body = call("GET", "/api/waybills/AWB-SMOKE/plan")
check("stored plan is the replacement", st == 200 and body["revision"] == 3
      and len(body["items"]) == 1, body)

print("ALL SMOKE CHECKS PASSED (incl. plan)")

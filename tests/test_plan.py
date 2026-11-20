"""Handover plan registration and reconciliation against the trusted prefix."""
import importlib

from conftest import handover, make_plan, make_waybill, plan_item, withdrawal


def put_plan(client, items, revision=1, wid="AWB-1"):
    return client.put(
        f"/api/waybills/{wid}/plan", json={"revision": revision, "items": items}
    )


def post_batch(client, scans, wid="AWB-1"):
    return client.post(f"/api/waybills/{wid}/scans/batch", json={"scans": scans})


THREE_ITEMS = [
    plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
    plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
    plan_item("D", "2026-11-18T11:00:00Z", "2026-11-18T12:00:00Z", False),
]


def test_plan_roundtrip_and_normalization(client):
    make_waybill(client)
    r = put_plan(
        client,
        [plan_item("B", "2026-11-18T15:00:00+08:00", "2026-11-18T17:00:00+08:00", True)],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_revision"] == 1
    assert body["plan"]["items"][0]["window_start"].startswith("2026-11-18T07:00:00")
    assert body["plan"]["items"][0]["window_end"].startswith("2026-11-18T09:00:00")

    got = client.get("/api/waybills/AWB-1/plan")
    assert got.status_code == 200
    assert got.json()["revision"] == 1
    assert got.json()["items"][0]["receiver"] == "B"


def test_missing_plan_reads_as_empty(client):
    make_waybill(client)
    r = client.get("/api/waybills/AWB-1/plan")
    assert r.status_code == 200
    assert r.json() == {"waybill_id": "AWB-1", "revision": None, "items": []}
    recon = client.get("/api/waybills/AWB-1/reconciliation").json()
    assert recon["has_plan"] is False
    assert recon["complete"] is False


def test_plan_validation(client):
    make_waybill(client)
    # 61 items -> 422
    many = [
        plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z") for _ in range(61)
    ]
    assert put_plan(client, many).status_code == 422
    # inverted window -> 422
    bad = [plan_item("B", "2026-11-18T09:00:00Z", "2026-11-18T07:00:00Z")]
    assert put_plan(client, bad).status_code == 422
    # naive datetime -> 422
    naive = [
        {
            "receiver": "B",
            "window_start": "2026-11-18T07:00:00",
            "window_end": "2026-11-18T09:00:00Z",
            "required": True,
        }
    ]
    assert put_plan(client, naive).status_code == 422
    # empty item list -> 422
    assert put_plan(client, []).status_code == 422
    # unknown waybill -> 404
    assert put_plan(client, THREE_ITEMS, wid="NOPE").status_code == 404


def test_plan_60_items_accepted(client):
    make_waybill(client)
    items = [
        plan_item("B", "2026-11-18T00:00:00Z", "2026-11-19T00:00:00Z") for _ in range(60)
    ]
    assert put_plan(client, items).status_code == 200


def test_plan_replacement_swaps_whole_plan(client):
    make_waybill(client)
    assert put_plan(client, THREE_ITEMS, revision=2).status_code == 200
    # same version may be re-registered: full replacement, never a merge
    r = put_plan(
        client,
        [plan_item("X", "2026-11-18T07:00:00Z", "2026-11-18T08:00:00Z")],
        revision=2,
    )
    assert r.status_code == 200
    body = client.get("/api/waybills/AWB-1/plan").json()
    assert body["revision"] == 2
    assert [i["receiver"] for i in body["items"]] == ["X"]

    # omitted revision auto-increments
    r = client.put(
        "/api/waybills/AWB-1/plan",
        json={
            "items": [
                plan_item("Y", "2026-11-18T07:00:00Z", "2026-11-18T08:00:00Z", True)
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["plan_revision"] == 3
    assert [i["receiver"] for i in r.json()["plan"]["items"]] == ["Y"]


def test_batch_receipt_carries_plan_revision_and_result(client):
    make_waybill(client)
    put_plan(client, THREE_ITEMS, revision=4)
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_revision"] == 4
    assert body["reconciliation"]["plan_revision"] == 4
    assert body["reconciliation"]["items"][0]["status"] == "CONFIRMED"
    assert body["reconciliation"]["items"][1]["status"] == "CONFIRMED"
    assert body["reconciliation"]["items"][2]["status"] == "UNSUPPORTED"
    assert body["reconciliation"]["required_covered"] is True
    assert body["reconciliation"]["complete"] is True
    # same snapshot in the chain view
    assert body["chain"]["plan_revision"] == 4
    assert body["chain"]["reconciliation"]["plan_revision"] == 4


def test_plan_replacement_receipt_carries_reconciliation(client):
    make_waybill(client)
    r = put_plan(client, THREE_ITEMS, revision=7)
    body = r.json()
    assert body["plan_revision"] == 7
    assert body["reconciliation"]["plan_revision"] == 7
    assert body["reconciliation"]["scans_considered"] == 0
    assert body["reconciliation"]["unresolved"] is None


def test_inclusive_window_bounds(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T08:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z", True),
        ],
    )
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),  # window start
            handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),  # shared bound
        ],
    )
    assert r.status_code == 200
    assert [s["status"] for s in r.json()["reconciliation"]["scans"]] == [
        "CONFIRMED",
        "CONFIRMED",
    ]


def test_unresolved_prefix_reports_earliest_failing_scan(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "WRONG"),  # no matching item
        ],
    )
    recon = r.json()["reconciliation"]
    assert recon["unresolved"] is not None
    assert recon["unresolved"]["scan_id"] == "S2"
    assert recon["unresolved"]["revision"] == 1
    assert recon["unresolved"]["prefix_length"] == 2
    assert recon["complete"] is False
    assert recon["match"]["correspondence_count"] == 0
    # per-item / per-scan statuses are omitted when nothing matches
    assert recon["items"] == [] and recon["scans"] == []


def test_already_unmatchable_first_scan(client):
    make_waybill(client)
    put_plan(client, [plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True)])
    r = post_batch(client, [handover("S1", 1, "2026-11-18T10:00:00Z", "ORIG", "B")])
    recon = r.json()["reconciliation"]
    assert recon["unresolved"]["scan_id"] == "S1"
    assert recon["unresolved"]["prefix_length"] == 1


def test_unresolved_clears_after_correction(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "WRONG"),
        ],
    )
    r = post_batch(
        client, [handover("S2", 2, "2026-11-18T10:00:00Z", "B", "C")]
    )
    recon = r.json()["reconciliation"]
    assert recon["unresolved"] is None
    assert recon["complete"] is True


def test_skipping_required_item_is_illegal(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            # no C scan, D isn't planned; second scan must map to item 2 -> no item
            handover("S2", 1, "2026-11-18T10:30:00Z", "B", "C"),
        ],
    )
    # S2 does map to item 1; skipping requires an unmatched non-required item in
    # between, so this remains resolvable.
    assert r.json()["reconciliation"]["unresolved"] is None

    # A trailing unmatched scan beyond the last item is unmatchable.
    r2 = post_batch(
        client,
        [handover("S3", 1, "2026-11-18T11:30:00Z", "C", "E")],
    )
    recon = r2.json()["reconciliation"]
    assert recon["unresolved"]["scan_id"] == "S3"


def test_optional_item_before_first_scan_can_be_skipped(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("A", "2026-11-18T06:00:00Z", "2026-11-18T07:00:00Z", False),
            plan_item("B", "2026-11-18T08:00:00Z", "2026-11-18T08:30:00Z", True),
        ],
    )
    r = post_batch(client, [handover("S1", 1, "2026-11-18T08:10:00Z", "ORIG", "B")])
    recon = r.json()["reconciliation"]
    assert recon["unresolved"] is None
    assert recon["items"][0]["status"] == "UNSUPPORTED"
    assert recon["items"][1]["status"] == "CONFIRMED"
    assert recon["required_covered"] is True


def test_optional_item_between_scans_can_be_skipped(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("X", "2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z", False),
            plan_item("C", "2026-11-18T10:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:30:00Z", "B", "C"),
        ],
    )
    recon = r.json()["reconciliation"]
    assert recon["unresolved"] is None
    assert [i["status"] for i in recon["items"]] == [
        "CONFIRMED",
        "UNSUPPORTED",
        "CONFIRMED",
    ]
    assert recon["required_covered"] is True


def test_trailing_required_items_may_be_left_for_later(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    r = post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")])
    recon = r.json()["reconciliation"]
    assert recon["unresolved"] is None
    assert recon["required_covered"] is False
    assert recon["complete"] is False
    assert recon["items"][1]["status"] == "UNSUPPORTED"


def test_ambiguous_matching_marks_possible(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T12:00:00Z", False),
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T12:00:00Z", False),
        ],
    )
    r = post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")])
    recon = r.json()["reconciliation"]
    assert recon["match"]["correspondence_count"] == 2
    assert recon["items"][0]["status"] == "POSSIBLE"
    assert recon["items"][1]["status"] == "POSSIBLE"
    assert recon["scans"][0]["status"] == "POSSIBLE"
    assert recon["scans"][0]["plan_indices"] == [0, 1]

    r2 = post_batch(
        client,
        [handover("S2", 1, "2026-11-18T09:00:00Z", "B", "B")],
    )
    recon2 = r2.json()["reconciliation"]
    # only one order-preserving assignment covers both scans
    assert recon2["match"]["correspondence_count"] == 1
    assert [i["status"] for i in recon2["items"]] == ["CONFIRMED", "CONFIRMED"]
    assert [s["status"] for s in recon2["scans"]] == ["CONFIRMED", "CONFIRMED"]


def test_required_item_cannot_be_skipped_when_ambiguous_windows(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T12:00:00Z", True),
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T12:00:00Z", True),
        ],
    )
    r = post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")])
    recon = r.json()["reconciliation"]
    # first required item blocks skipping, so S1 can only map to item 0
    assert recon["match"]["correspondence_count"] == 1
    assert recon["items"][0]["status"] == "CONFIRMED"
    assert recon["items"][1]["status"] == "UNSUPPORTED"
    assert recon["required_covered"] is False


def test_chain_break_prevents_completion(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
            plan_item("C", "2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z", True),
        ],
    )
    r = post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "X", "C"),  # GAP
        ],
    )
    recon = r.json()["reconciliation"]
    assert recon["chain_intact"] is False
    # only the trusted prefix (S1) is considered
    assert recon["scans_considered"] == 1
    assert recon["required_covered"] is False
    assert recon["complete"] is False


def test_withdrawn_scan_no_longer_counts(client):
    make_waybill(client)
    put_plan(
        client,
        [
            plan_item("B", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z", True),
        ],
    )
    post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")])
    assert client.get("/api/waybills/AWB-1/reconciliation").json()["complete"] is True
    r = post_batch(client, [withdrawal("S1", 2)])
    recon = r.json()["reconciliation"]
    assert recon["scans_considered"] == 0
    assert recon["complete"] is False


def test_failed_batch_keeps_plan_and_reconciliation(client):
    make_waybill(client)
    put_plan(client, THREE_ITEMS, revision=1)
    post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")])
    before = client.get("/api/waybills/AWB-1/reconciliation").json()

    r = post_batch(client, [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "OTHER")])
    assert r.status_code == 409
    after = client.get("/api/waybills/AWB-1/reconciliation").json()
    assert after == before
    assert client.get("/api/waybills/AWB-1/plan").json()["revision"] == 1


def test_reconciliation_survives_restart(client):
    make_waybill(client)
    put_plan(client, THREE_ITEMS, revision=5)
    post_batch(
        client,
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),
        ],
    )
    expected = client.get("/api/waybills/AWB-1/reconciliation").json()

    from fastapi.testclient import TestClient

    from app import config, database, main

    db_path = config.DB_PATH
    importlib.reload(main)
    with TestClient(main.app) as new_client:
        again = new_client.get("/api/waybills/AWB-1/reconciliation").json()
        assert again == expected
        assert again["complete"] is True
        plan = new_client.get("/api/waybills/AWB-1/plan").json()
        assert plan["revision"] == 5
        assert len(plan["items"]) == 3
    assert db_path.exists()


def test_waybill_without_plan_still_works(client):
    make_waybill(client)
    r = post_batch(
        client,
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    assert r.status_code == 200
    assert r.json()["plan_revision"] is None
    assert r.json()["chain"]["chain_intact"] is True
    assert client.get("/api/waybills/AWB-1/chain").json()["current_custodian"] == "B"

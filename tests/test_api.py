"""End-to-end self tests via FastAPI TestClient + a temp-file SQLite DB."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def ctx():
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "test.db")
    settings = Settings(db_path=db_path, fail_device=None)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, db_path, tmp
    tmp.cleanup()


def reading(source, rev, device, ts, meter, kwh):
    return {
        "source_id": source, "revision": rev, "device_id": device,
        "ts": ts, "meter_no": meter, "cumulative_kwh": kwh,
    }


def handover(source, rev, device, ts, old_m, old_v, new_m, new_v):
    return {
        "source_id": source, "revision": rev, "device_id": device, "ts": ts,
        "old_meter_no": old_m, "old_final_kwh": old_v,
        "new_meter_no": new_m, "new_initial_kwh": new_v,
    }


def current_intervals(client, device=None):
    params = {"status": "current"}
    if device:
        params["device_id"] = device
    return client.get("/intervals", params=params).json()


def all_intervals(client, device=None):
    params = {"status": "all"}
    if device:
        params["device_id"] = device
    return client.get("/intervals", params=params).json()


# ---------------------------------------------------------------------------
# 1. Basic same-meter difference, provenance, undeterminable is never zero
# ---------------------------------------------------------------------------

def test_same_meter_difference_and_provenance(ctx):
    client, _, _ = ctx
    r = client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    assert r.status_code == 201 and r.json()["accepted"]
    r = client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 130.5))
    assert r.status_code == 201
    intervals = current_intervals(client, "D1")
    assert len(intervals) == 1
    iv = intervals[0]
    assert iv["energy_kwh"] == pytest.approx(30.5)
    assert iv["undeterminable"] is False
    assert iv["start_source_id"] == "S1" and iv["end_source_id"] == "S2"
    assert iv["start_revision"] == 1 and iv["end_revision"] == 1


def test_backward_reading_undeterminable_not_zero(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 90.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None          # never reported as zero
    assert iv["undeterminable"] is True
    assert iv["reason"].startswith("negative_difference")
    assert iv["status"] == "current"          # still a valid current row


def test_zero_difference_is_real_zero(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 50.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 50.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] == 0.0 and iv["undeterminable"] is False


# ---------------------------------------------------------------------------
# 2. Cross-meter intervals and handovers
# ---------------------------------------------------------------------------

def test_cross_meter_settled_with_unique_handover(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    # Without a handover yet: cross-meter interval is undeterminable.
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None and iv["reason"].startswith("handover_missing")

    # Submit the handover at 01:00 with old final 150 / new initial 10.
    r = client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert r.status_code == 201
    intervals = current_intervals(client, "D1")
    assert len(intervals) == 1
    iv = intervals[0]
    # (150-100) + (40-10) = 80
    assert iv["energy_kwh"] == pytest.approx(80.0)
    assert iv["handover_source_id"] == "H1" and iv["handover_revision"] == 1


def test_cross_meter_wrong_meter_pair_still_missing(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M2", 40.0))
    # Handover mentions a different meter pair -> no unique match.
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M1", 150.0, "M3", 10.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None and iv["reason"].startswith("handover_missing")


def test_negative_segment_relative_to_handover(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 5.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    iv = current_intervals(client, "D1")[0]
    # second segment 5 - 10 < 0 -> undeterminable
    assert iv["energy_kwh"] is None and iv["reason"].startswith("negative_segment")


# ---------------------------------------------------------------------------
# 3. Revision rules
# ---------------------------------------------------------------------------

def test_revision_conflict_and_rollback_and_idempotency(ctx):
    client, _, _ = ctx
    payload = reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0)
    assert client.post("/readings", json=payload).status_code == 201

    # Same key, different content -> 409
    conflict = reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 110.0)
    r = client.post("/readings", json=conflict)
    assert r.status_code == 409 and "content conflict" in r.json()["detail"]

    # Identical re-submission -> idempotent 201, no new affected device
    r = client.post("/readings", json=payload)
    assert r.status_code == 201 and r.json()["idempotent"] is True

    # Rollback (revision 0 / lower than latest) -> 409
    r = client.post(
        "/readings",
        json=reading("S2", 2, "D1", "2026-01-01T01:00:00Z", "M1", 120.0),
    )
    assert r.status_code == 409 and "first revision" in r.json()["detail"]

    client.post("/readings", json=reading("S3", 1, "D1", "2026-01-01T01:00:00Z", "M1", 120.0))
    r = client.post(
        "/readings",
        json=reading("S3", 1, "D1", "2026-01-01T01:00:00Z", "M1", 125.0),
    )
    assert r.status_code == 409
    # Skipping revision 2 (latest is 1, posting 3) is rejected as a
    # non-consecutive revision, not treated as the first submission.
    r = client.post(
        "/readings",
        json=reading("S3", 3, "D1", "2026-01-01T01:00:00Z", "M1", 125.0),
    )
    assert r.status_code == 409 and "non-consecutive" in r.json()["detail"]
    # The correct next revision works.
    r = client.post(
        "/readings",
        json=reading("S3", 2, "D1", "2026-01-01T01:00:00Z", "M1", 125.0),
    )
    assert r.status_code == 201 and r.json()["idempotent"] is False
    # And a rollback after that is rejected too.
    r = client.post(
        "/readings",
        json=reading("S3", 2, "D1", "2026-01-01T01:00:00Z", "M1", 126.0),
    )
    assert r.status_code == 409 and "content conflict" in r.json()["detail"]


def test_handover_revision_rules(ctx):
    client, _, _ = ctx
    h = handover("H1", 1, "D1", "2026-01-01T01:00:00Z", "M1", 150.0, "M2", 10.0)
    assert client.post("/handovers", json=h).status_code == 201
    h2 = dict(h, old_final_kwh=160.0)
    assert client.post("/handovers", json=h2).status_code == 409
    assert client.post("/handovers", json=h).json()["idempotent"] is True
    rollback = dict(h, revision=0)
    # pydantic rejects revision 0 at validation layer
    assert client.post("/handovers", json=rollback).status_code == 422


# ---------------------------------------------------------------------------
# 4. Late submissions invalidate old intervals; retry completes rebuild
# ---------------------------------------------------------------------------

def test_late_reading_stales_old_intervals_and_rebuild(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    first = current_intervals(client, "D1")
    assert len(first) == 1 and first[0]["energy_kwh"] == pytest.approx(30.0)
    first_id = first[0]["id"]

    # Late reading at 01:00 -> old interval immediately loses current eligibility.
    r = client.post("/readings", json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0))
    assert r.status_code == 201
    stale = [i for i in all_intervals(client, "D1") if i["id"] == first_id]
    assert stale and stale[0]["status"] == "stale"
    currents = current_intervals(client, "D1")
    assert {i["id"] for i in currents} != {first_id}
    assert sorted(i["energy_kwh"] for i in currents) == pytest.approx([12.0, 18.0])


def test_corrected_reading_revision_rebuilds(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    assert current_intervals(client, "D1")[0]["energy_kwh"] == pytest.approx(30.0)

    # Correct S2 with revision 2.
    r = client.post("/readings", json=reading("S2", 2, "D1", "2026-01-01T02:00:00Z", "M1", 145.0))
    assert r.status_code == 201 and r.json()["idempotent"] is False
    iv = current_intervals(client, "D1")
    assert len(iv) == 1 and iv[0]["energy_kwh"] == pytest.approx(45.0)
    assert iv[0]["end_revision"] == 2
    # The old computed row is retained as stale.
    assert any(i["status"] == "stale" for i in all_intervals(client, "D1"))


def test_rebuild_failure_keeps_stale_and_retry_succeeds(ctx):
    client, db_path, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))

    # Force the auto-rebuild after a late reading to fail (test hook header).
    r = client.post(
        "/readings",
        headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["rebuild"]["success"] is False
    assert body["rebuild"]["failed_devices"]

    # No current intervals for D1; old rows survive as stale (not resurrected).
    assert current_intervals(client, "D1") == []
    stale = all_intervals(client, "D1")
    assert stale and all(i["status"] == "stale" for i in stale)
    assert any(i["energy_kwh"] == pytest.approx(30.0) for i in stale)

    # Retry rebuild without the fault -> completes, current data comes back.
    r = client.post("/rebuild", params={"device_id": "D1"})
    assert r.status_code == 200 and r.json()["success"] is True
    currents = current_intervals(client, "D1")
    assert len(currents) == 2
    assert sorted(i["energy_kwh"] for i in currents) == pytest.approx([12.0, 18.0])

    # Audit trail recorded both the failed run and the success.
    runs = client.get("/rebuild-runs").json()
    statuses = [row["status"] for row in runs]
    assert "failed" in statuses and "success" in statuses


def test_restart_does_not_resurrect_stale_intervals(ctx):
    client, db_path, tmp = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    # Make D1 stale with a failing rebuild.
    client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )
    assert current_intervals(client, "D1") == []

    # Simulate a full process restart against the same DB file.
    app2 = create_app(Settings(db_path=db_path, fail_device=None))
    with TestClient(app2) as client2:
        # No auto-rebuild on boot: still no current intervals.
        assert current_intervals(client2, "D1") == []
        runs = client2.get("/rebuild-runs").json()
        assert any(row["status"] == "interrupted" for row in runs)
        # Manual retry completes the rebuild.
        r = client2.post("/rebuild", params={"device_id": "D1"})
        assert r.json()["success"] is True
        assert len(current_intervals(client2, "D1")) == 2


def test_unrelated_device_keeps_serving(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    client.post("/readings", json=reading("K1", 1, "D2", "2026-01-01T00:00:00Z", "N1", 200.0))
    client.post("/readings", json=reading("K2", 1, "D2", "2026-01-01T02:00:00Z", "N1", 225.0))

    # Failing rebuild for D1 must not touch D2.
    client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )
    assert current_intervals(client, "D1") == []
    d2 = current_intervals(client, "D2")
    assert len(d2) == 1 and d2[0]["energy_kwh"] == pytest.approx(25.0)

    # Retry only D1; D2 remains available throughout.
    assert client.post("/rebuild", params={"device_id": "D1"}).json()["success"] is True
    assert len(current_intervals(client, "D1")) == 2
    assert len(current_intervals(client, "D2")) == 1


# ---------------------------------------------------------------------------
# 5. Handover correction and end-to-end multi-interval chain
# ---------------------------------------------------------------------------

def test_reading_correction_moved_to_other_device_stops_old_supply(ctx):
    """Only the newest revision of a source may order readings on any device."""
    client, _, _ = ctx
    client.post("/readings", json=reading("A", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 1, "D1", "2026-01-01T01:00:00Z", "M1", 110.0))
    client.post("/readings", json=reading("C", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    before = current_intervals(client, "D1")
    assert len(before) == 2
    old_ids = {i["id"] for i in before}

    # Correction: B rev2 now belongs to D2.  D1's old intervals lose current
    # eligibility immediately, and the rebuild must not use B rev1 for ordering.
    r = client.post("/readings", json=reading("B", 2, "D2", "2026-01-01T01:00:00Z", "M1", 110.0))
    assert r.status_code == 201
    assert sorted(r.json()["affected_devices"]) == ["D1", "D2"]

    stale = [i for i in all_intervals(client, "D1") if i["id"] in old_ids]
    assert len(stale) == 2 and all(i["status"] == "stale" for i in stale)

    d1 = current_intervals(client, "D1")
    assert len(d1) == 1  # A -> C only; the moved source is no longer present
    assert d1[0]["energy_kwh"] == pytest.approx(30.0)
    assert "B" not in {d1[0]["start_source_id"], d1[0]["end_source_id"]}
    # D2 owns the corrected reading but has no adjacent pair yet.
    assert current_intervals(client, "D2") == []


def test_corrected_reading_supplies_only_new_device(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("A", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 1, "D1", "2026-01-01T01:00:00Z", "M1", 110.0))
    # Move B to D2 and give D2 a neighbour so its ordering can be checked.
    client.post("/readings", json=reading("X", 1, "D2", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 2, "D2", "2026-01-01T01:00:00Z", "M1", 110.0))
    client.post("/readings", json=reading("Y", 1, "D2", "2026-01-01T02:00:00Z", "M1", 130.0))

    d2 = current_intervals(client, "D2")
    assert len(d2) == 2
    assert [(i["start_source_id"], i["end_source_id"]) for i in d2] == [
        ("X", "B"), ("B", "Y")
    ]
    assert d2[0]["end_revision"] == 2 and d2[1]["start_revision"] == 2
    assert current_intervals(client, "D1") == []


def test_handover_correction_to_other_meter_pair_revokes_old_settlement(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    settled = current_intervals(client, "D1")[0]
    assert settled["energy_kwh"] == pytest.approx(80.0)
    settled_id = settled["id"]

    # Correct the handover to a different meter pair.  Its revision 1 must no
    # longer match the M-old -> M-new interval, and must not settle it at all.
    r = client.post("/handovers", json=handover(
        "H1", 2, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M3", 10.0))
    assert r.status_code == 201

    old = [i for i in all_intervals(client, "D1") if i["id"] == settled_id]
    assert old and old[0]["status"] == "stale"   # still available for audit
    iv = current_intervals(client, "D1")
    assert len(iv) == 1
    assert iv[0]["energy_kwh"] is None
    assert iv[0]["undeterminable"] is True
    assert iv[0]["reason"].startswith("handover_missing")
    assert iv[0]["handover_source_id"] is None


def test_handover_correction_moving_device_stops_old_device_match(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    r = client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert r.status_code == 201
    assert current_intervals(client, "D1")[0]["energy_kwh"] == pytest.approx(80.0)

    # Revise the handover onto another device: it can no longer settle D1.
    r = client.post("/handovers", json=handover(
        "H1", 2, "D2", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert r.status_code == 201
    assert sorted(r.json()["affected_devices"]) == ["D1", "D2"]
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None and iv["reason"].startswith("handover_missing")


def test_failed_rebuild_after_correction_never_revives_old_revision(ctx):
    client, db_path, _ = ctx
    client.post("/readings", json=reading("A", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    assert current_intervals(client, "D1")[0]["energy_kwh"] == pytest.approx(30.0)

    # Correction moving B to D2, but both rebuilds fail before commit.
    r = client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("B", 2, "D2", "2026-01-01T02:00:00Z", "M1", 130.0),
    )
    assert r.status_code == 201
    assert r.json()["rebuild"]["success"] is False
    assert set(r.json()["rebuild"]["failed_devices"]) == {"D1", "D2"}
    assert current_intervals(client, "D1") == []
    assert current_intervals(client, "D2") == []
    stale = all_intervals(client, "D1")
    assert stale and all(i["status"] == "stale" for i in stale)
    assert all("B" not in {i["start_source_id"], i["end_source_id"]} or i["end_revision"] == 1
               for i in stale)

    # Restart without retrying: still no current intervals, nothing resurrected.
    app2 = create_app(Settings(db_path=db_path, fail_device=None))
    with TestClient(app2) as client2:
        assert current_intervals(client2, "D1") == []
        assert current_intervals(client2, "D2") == []

        # Retry only rebuilds from the *now-authoritative* revisions: B rev1 is
        # gone from D1, so D1 has no adjacent pair and yields no interval.
        r = client2.post("/rebuild", params={"device_id": "D1"})
        assert r.json()["success"] is True
        assert current_intervals(client2, "D1") == []
        r = client2.post("/rebuild", params={"device_id": "D2"})
        assert r.json()["success"] is True
        assert current_intervals(client2, "D2") == []

        # Audit history remains queryable.
        old = [i for i in all_intervals(client2, "D1") if i["status"] == "stale"]
        assert any(i["energy_kwh"] == pytest.approx(30.0) for i in old)
        assert any(i["end_source_id"] == "B" and i["end_revision"] == 1 for i in old)


def test_default_retry_includes_device_pending_without_stale_rows(ctx):
    """A source corrected into a device with no prior pair still needs a retry.

    That device has no stale interval row (nothing was current there before),
    so the pending marker — not the stale row set — must make the default
    ``POST /rebuild`` include it.
    """
    client, _, _ = ctx
    client.post("/readings", json=reading("A", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    # D2 has a single reading, hence no intervals and no stale rows.
    client.post("/readings", json=reading("X", 1, "D2", "2026-01-01T00:00:00Z", "M1", 100.0))
    assert current_intervals(client, "D2") == []

    r = client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("B", 2, "D2", "2026-01-01T02:00:00Z", "M1", 130.0),
    )
    assert r.status_code == 201 and r.json()["rebuild"]["success"] is False
    assert current_intervals(client, "D2") == []
    d2_stale = [i for i in all_intervals(client, "D2") if i["status"] == "stale"]
    assert d2_stale == []      # nothing to mark stale, but the device is pending

    # Default rebuild (no device_id) must pick D2 up and derive from B rev2.
    r = client.post("/rebuild")
    assert r.json()["success"] is True
    d2 = current_intervals(client, "D2")
    assert len(d2) == 1
    assert d2[0]["start_source_id"] == "X" and d2[0]["end_source_id"] == "B"
    assert d2[0]["end_revision"] == 2
    assert d2[0]["energy_kwh"] == pytest.approx(30.0)


def test_handover_correction_failed_rebuild_then_retry_uses_new_revision(ctx):
    client, db_path, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert current_intervals(client, "D1")[0]["energy_kwh"] == pytest.approx(80.0)

    # Correction to a different meter pair; rebuild fails so stale rows remain.
    client.post(
        "/handovers", headers={"X-Fail-Rebuild": "true"},
        json=handover("H1", 2, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M3", 10.0),
    )
    assert current_intervals(client, "D1") == []

    # Process restart must not make the old revision eligible again.
    app2 = create_app(Settings(db_path=db_path, fail_device=None))
    with TestClient(app2) as client2:
        assert current_intervals(client2, "D1") == []
        assert client2.post("/rebuild", params={"device_id": "D1"}).json()["success"] is True
        iv = current_intervals(client2, "D1")
        assert len(iv) == 1
        assert iv[0]["energy_kwh"] is None
        assert iv[0]["reason"].startswith("handover_missing")


def test_correction_does_not_affect_unrelated_devices(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("A", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("B", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    client.post("/readings", json=reading("K1", 1, "D2", "2026-01-01T00:00:00Z", "N1", 200.0))
    client.post("/readings", json=reading("K2", 1, "D2", "2026-01-01T02:00:00Z", "N1", 230.0))
    before = current_intervals(client, "D2")

    client.post("/readings", json=reading("B", 2, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    after = current_intervals(client, "D2")
    assert [(i["id"], i["energy_kwh"], i["status"]) for i in after] == [
        (i["id"], i["energy_kwh"], i["status"]) for i in before
    ]
    assert after[0]["energy_kwh"] == pytest.approx(30.0)


def test_ambiguous_handovers_still_undeterminable_but_superseded_ones_ignored(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T00:30:00Z", "M-old", 150.0, "M-new", 10.0))
    client.post("/handovers", json=handover(
        "H2", 1, "D1", "2026-01-01T01:30:00Z", "M-old", 150.0, "M-new", 10.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None and iv["reason"].startswith("ambiguous_handover")

    # If H2's latest revision no longer matches, H1 is the single valid one.
    client.post("/handovers", json=handover(
        "H2", 2, "D1", "2026-01-01T01:30:00Z", "M-old", 150.0, "M3", 10.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] == pytest.approx(80.0)
    assert iv["handover_source_id"] == "H1" and iv["handover_revision"] == 1

    # Conversely, H1's superseded revision must not keep the pair ambiguous.
    client.post("/handovers", json=handover(
        "H1", 2, "D1", "2026-01-01T00:30:00Z", "M-old", 150.0, "M4", 10.0))
    iv = current_intervals(client, "D1")[0]
    assert iv["energy_kwh"] is None and iv["reason"].startswith("handover_missing")


def test_handover_correction_revises_settlement(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert current_intervals(client, "D1")[0]["energy_kwh"] == pytest.approx(80.0)

    # Correct the handover (old final actually 160): (160-100)+(40-10)=90.
    client.post("/handovers", json=handover(
        "H1", 2, "D1", "2026-01-01T01:00:00Z", "M-old", 160.0, "M-new", 10.0))
    iv = current_intervals(client, "D1")
    assert len(iv) == 1 and iv[0]["energy_kwh"] == pytest.approx(90.0)
    assert iv[0]["handover_revision"] == 2


def test_full_chain_with_two_meters(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 130.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:30:00Z", "M1", 140.0, "M2", 5.0))
    client.post("/readings", json=reading("S3", 1, "D1", "2026-01-01T02:00:00Z", "M2", 20.0))
    client.post("/readings", json=reading("S4", 1, "D1", "2026-01-01T03:00:00Z", "M2", 35.0))

    iv = current_intervals(client, "D1")
    assert len(iv) == 3
    assert iv[0]["energy_kwh"] == pytest.approx(30.0)               # same meter
    assert iv[1]["energy_kwh"] == pytest.approx((140 - 130) + (20 - 5))  # 25
    assert iv[2]["energy_kwh"] == pytest.approx(15.0)
    assert iv[1]["handover_source_id"] == "H1"


def test_validation_rejects_negative_cumulative(ctx):
    client, _, _ = ctx
    r = client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", -1.0))
    assert r.status_code == 422


def test_health_and_openapi(ctx):
    client, _, _ = ctx
    assert client.get("/health").json()["status"] == "ok"
    spec = client.get("/openapi.json").json()
    assert "/readings" in spec["paths"] and "/handovers" in spec["paths"]
    assert client.get("/docs").status_code == 200

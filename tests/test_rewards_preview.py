"""Tests for POST /rewards/preview: tie-aware prize split projection."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "rewards-test.db"))
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _preview(client, prize_cents, **extra):
    body = {"prize_cents": prize_cents}
    body.update(extra)
    r = client.post("/rewards/preview", json=body)
    assert r.status_code == 200, body
    return r.json()


def test_empty_ledger_returns_revision_zero_and_no_winners(client):
    body = _preview(client, [100, 50])
    assert body == {"revision": 0, "winners": []}


def test_no_ties_prizes_map_one_to_one_to_ranks(client):
    _post(client, "e1", "alice", 30)
    _post(client, "e2", "bob", 20)
    _post(client, "e3", "carol", 10)
    _post(client, "e4", "dave", 5)  # rank 4: outside the prize zone
    body = _preview(client, [1000, 500, 250])
    assert body["revision"] == 4
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 30, "prize_cents": 1000},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 2,
         "total_points": 20, "prize_cents": 500},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 10, "prize_cents": 250},
    ]


def test_prize_zone_longer_than_field_leaves_extra_prizes_unpaid(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    body = _preview(client, [100, 50, 25])
    assert [w["prize_cents"] for w in body["winners"]] == [100, 50]


def test_tie_group_pools_covered_prizes_and_splits_evenly(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 5)
    body = _preview(client, [100, 50, 25])
    # {alice, bob} covers ranks 1-2: pool 100+50=150, split 75/75.
    # carol takes rank 3 (ranks continue after the group).
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "bob", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 5, "prize_cents": 25},
    ]


def test_leftover_cents_go_to_lowest_participant_ids(client):
    _post(client, "e1", "carol", 10)
    _post(client, "e2", "alice", 10)
    _post(client, "e3", "bob", 10)
    body = _preview(client, [100, 0, 1])
    # pool = 100+0+1 = 101 over 3 members: 33 each + 2 leftover cents,
    # handed out in participant_id ascending order (alice, bob).
    assert [(w["participant_id"], w["prize_cents"]) for w in body["winners"]] == [
        ("alice", 34),
        ("bob", 34),
        ("carol", 33),
    ]
    assert sum(w["prize_cents"] for w in body["winners"]) == 101


def test_group_straddling_zone_edge_splits_only_in_zone_prizes(client):
    _post(client, "e1", "alice", 20)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 10)
    _post(client, "e4", "dave", 5)
    body = _preview(client, [100, 50])
    # {bob, carol} covers ranks 2-3 but only rank 2 is in the zone:
    # pool 50 split across the whole group; dave (rank 4) wins nothing.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 20, "prize_cents": 100},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 3,
         "total_points": 10, "prize_cents": 25},
        {"participant_id": "carol", "rank_start": 2, "rank_end": 3,
         "total_points": 10, "prize_cents": 25},
    ]


def test_group_fully_beyond_zone_yields_no_winners(client):
    _post(client, "e1", "alice", 20)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 10)
    body = _preview(client, [100])
    assert [(w["participant_id"], w["prize_cents"]) for w in body["winners"]] == [
        ("alice", 100),
    ]


def test_three_way_tie_behind_a_solo_leader(client):
    _post(client, "e1", "alice", 30)
    _post(client, "e2", "bob", 20)
    _post(client, "e3", "carol", 20)
    _post(client, "e4", "dave", 20)
    body = _preview(client, [1000, 300])
    # {bob, carol, dave} covers ranks 2-4; only rank 2 is in the zone,
    # so 300 is split three ways with no remainder.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 30, "prize_cents": 1000},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 4,
         "total_points": 20, "prize_cents": 100},
        {"participant_id": "carol", "rank_start": 2, "rank_end": 4,
         "total_points": 20, "prize_cents": 100},
        {"participant_id": "dave", "rank_start": 2, "rank_end": 4,
         "total_points": 20, "prize_cents": 100},
    ]


def test_zero_prizes_still_list_zone_members(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    body = _preview(client, [0, 0])
    assert [(w["participant_id"], w["prize_cents"]) for w in body["winners"]] == [
        ("alice", 0),
        ("bob", 0),
    ]


def test_historical_revision_pins_the_snapshot(client):
    _post(client, "e1", "alice", 10)  # rev1
    _post(client, "e2", "bob", 20)  # rev2

    body = _preview(client, [100, 50], revision=1)
    assert body["revision"] == 1
    assert [(w["participant_id"], w["prize_cents"]) for w in body["winners"]] == [
        ("alice", 100),
    ]

    body = _preview(client, [100, 50])  # defaults to the latest revision
    assert body["revision"] == 2
    assert [(w["participant_id"], w["prize_cents"]) for w in body["winners"]] == [
        ("bob", 100),
        ("alice", 50),
    ]


def test_correction_chain_counts_only_the_tip(client):
    _post(client, "e1", "alice", 10)  # rev1
    _post(client, "e2", "bob", 7)  # rev2
    _post(client, "e3", "alice", 15, correction_of="e1")  # rev3: alice 10 -> 15

    body = _preview(client, [100, 50])
    assert body["revision"] == 3
    assert [
        (w["participant_id"], w["total_points"], w["prize_cents"])
        for w in body["winners"]
    ] == [("alice", 15, 100), ("bob", 7, 50)]

    body = _preview(client, [100, 50], revision=2)
    assert [
        (w["participant_id"], w["total_points"], w["prize_cents"])
        for w in body["winners"]
    ] == [("alice", 10, 100), ("bob", 7, 50)]


def test_preview_does_not_write_to_the_ledger(client):
    _post(client, "e1", "alice", 10)
    before_revision = client.get("/leaderboard").json()["revision"]
    before_events = client.get("/events").json()

    body = _preview(client, [100, 50], revision=1)
    assert body["revision"] == 1

    assert client.get("/leaderboard").json()["revision"] == before_revision
    assert client.get("/events").json() == before_events


def test_revision_out_of_range_returns_404(client):
    _post(client, "e1", "alice", 10)
    for revision in (0, -1, 2, 99999):
        r = client.post(
            "/rewards/preview",
            json={"revision": revision, "prize_cents": [100]},
        )
        assert r.status_code == 404, revision


def test_prize_cents_validation_errors_return_422(client):
    _post(client, "e1", "alice", 10)
    bad_bodies = [
        {},  # prize_cents missing
        {"prize_cents": []},  # below the 1-item minimum
        {"prize_cents": [1] * 101},  # above the 100-item maximum
        {"prize_cents": [-1]},  # negative prize
        {"prize_cents": [100, -5, 0]},  # negative anywhere in the array
        {"prize_cents": [1.5]},  # non-integer
        {"prize_cents": "100"},  # not an array
    ]
    for body in bad_bodies:
        r = client.post("/rewards/preview", json=body)
        assert r.status_code == 422, body


def test_prize_cents_boundary_of_100_items_is_accepted(client):
    _post(client, "e1", "alice", 10)
    body = _preview(client, [0] * 99 + [7])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 10, "prize_cents": 0},
    ]


def test_non_integer_revision_returns_422(client):
    _post(client, "e1", "alice", 10)
    r = client.post(
        "/rewards/preview",
        json={"revision": "abc", "prize_cents": [100]},
    )
    assert r.status_code == 422

"""Functional tests for partition readiness recording and checking."""


def _register_basic(client):
    # s1, s2 -> d1 ; s3 standalone
    # r1 groups: [d1] / [s3]
    # r2 group : [s1, s2]
    client.post("/lineage/register", json={"nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "s3", "kind": "source"},
        {"name": "d1", "kind": "derived",
         "candidate_groups": [["s1", "s2"]]},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["d1"], ["s3"]]},
        {"name": "r2", "kind": "report",
         "candidate_groups": [["s1", "s2"]]},
    ]}).raise_for_status()


def _post_partitions(client, records, expected=200):
    resp = client.post("/readiness/partitions", json={"records": records})
    assert resp.status_code == expected, resp.text
    return resp.json()


def _check(client, column, date):
    resp = client.post(
        "/readiness/check",
        json={"report_column": column, "biz_date": date},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Recording validation
# --------------------------------------------------------------------------- #

def test_unregistered_and_non_source_fields_reject_whole_batch(client):
    _register_basic(client)
    resp = client.post("/readiness/partitions", json={"records": [
        {"field_name": "ghost", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ]})
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    assert resp.json()["details"]["unregistered_fields"] == ["ghost"]

    # derived / report nodes are not source fields.
    resp = client.post("/readiness/partitions", json={"records": [
        {"field_name": "d1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "r1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ]})
    assert resp.status_code == 422
    assert resp.json()["details"]["non_source_fields"] == ["d1", "r1"]

    # A good record bundled with a bad one must also be rolled back.
    resp = client.post("/readiness/partitions", json={"records": [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "ghost", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ]})
    assert resp.status_code == 422
    data = _check(client, "r2", "2026-11-14")
    assert data["ready"] is False
    assert {m["name"] for g in data["blocked_groups"]
            for m in g["missing_partitions"]} == {"s1", "s2"}


def test_same_revision_status_conflict_rejects_batch(client):
    _register_basic(client)
    # Conflict inside one batch.
    resp = client.post("/readiness/partitions", json={"records": [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 2, "status": "published"},
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 2, "status": "withdrawn"},
    ]})
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert resp.json()["details"]["revision_conflicts"][0]["field_name"] == "s1"

    # Conflict with a previously stored revision.
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ])
    resp = client.post("/readiness/partitions", json={"records": [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "withdrawn"},
    ]})
    assert resp.status_code == 409
    detail = resp.json()["details"]["revision_conflicts"][0]
    assert detail["stored_status"] == "published"
    assert detail["requested_status"] == "withdrawn"

    # Stored state must remain the original published revision: s1 is still
    # published, so only the never-recorded s2 is missing.
    data = _check(client, "r2", "2026-11-14")
    missing = {m["name"]: m for g in data["blocked_groups"]
               for m in g["missing_partitions"]}
    assert set(missing) == {"s2"}
    assert missing["s2"]["status"] == "unpublished"


def test_lower_revision_never_rolls_back_and_same_revision_is_idempotent(client):
    _register_basic(client)
    resp = _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 3, "status": "published"},
    ])
    assert resp["applied"] == 1

    # Lower revision retransmission is accepted but ignored.
    resp = _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "withdrawn"},
    ])
    assert resp["applied"] == 0
    assert resp["superseded"] == 1
    assert resp["results"][0]["outcome"] == "superseded"

    # Same revision + same status is a no-op.
    resp = _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 3, "status": "published"},
    ])
    assert resp["applied"] == 0
    assert resp["unchanged"] == 1
    assert resp["results"][0]["outcome"] == "unchanged"

    # Effective state is still the rev-3 published partition.
    data = _check(client, "r2", "2026-11-14")
    missing = {m["name"]: m for g in data["blocked_groups"]
               for m in g["missing_partitions"]}
    assert set(missing) == {"s2"}


def test_duplicate_key_same_status_in_batch_is_folded(client):
    _register_basic(client)
    resp = _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ])
    assert resp["received"] == 2
    assert resp["applied"] == 1
    assert len(resp["results"]) == 1


# --------------------------------------------------------------------------- #
# Readiness semantics
# --------------------------------------------------------------------------- #

def test_no_records_means_unpublished_and_blocked(client):
    _register_basic(client)
    data = _check(client, "r1", "2026-11-14")
    assert data["ready"] is False
    assert data["available_groups"] == []
    assert len(data["blocked_groups"]) == 2
    # Every blocked group lists the concrete missing source partitions.
    all_missing = {m["name"] for g in data["blocked_groups"]
                   for m in g["missing_partitions"]}
    assert all_missing == {"s1", "s2", "s3"}
    for group in data["blocked_groups"]:
        for part in group["missing_partitions"]:
            assert part["status"] == "unpublished"
            assert part["revision"] is None


def test_full_path_available_and_alternative_group(client):
    _register_basic(client)
    _post_partitions(client, [
        {"field_name": "s3", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ])
    # r1 can be produced via the alternative [s3] group.
    data = _check(client, "r1", "2026-11-14")
    assert data["ready"] is True
    assert data["available_groups"] == [1]
    assert len(data["blocked_groups"]) == 1
    blocked = data["blocked_groups"][0]
    assert blocked["group_index"] == 0
    assert blocked["inputs"] == ["d1"]
    assert {m["name"] for m in blocked["missing_partitions"]} == {"s1", "s2"}

    # r2 has no alternative and stays blocked.
    assert _check(client, "r2", "2026-11-14")["ready"] is False

    # Publish the remaining sources -> every group is usable.
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "s2", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ])
    data = _check(client, "r1", "2026-11-14")
    assert data["ready"] is True
    assert data["available_groups"] == [0, 1]
    assert data["blocked_groups"] == []
    assert _check(client, "r2", "2026-11-14")["ready"] is True


def test_withdrawal_at_higher_reversion_forces_fallback_and_blocks(client):
    _register_basic(client)
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "s2", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
        {"field_name": "s3", "biz_date": "2026-11-14",
         "revision": 1, "status": "published"},
    ])
    assert _check(client, "r1", "2026-11-14")["ready"] is True

    # Withdraw s3 at a higher revision: r1 must fall back to the [d1] group.
    _post_partitions(client, [
        {"field_name": "s3", "biz_date": "2026-11-14",
         "revision": 2, "status": "withdrawn"},
    ])
    data = _check(client, "r1", "2026-11-14")
    assert data["ready"] is True
    assert data["available_groups"] == [0]
    blocked = data["blocked_groups"][0]
    assert blocked["group_index"] == 1
    missing = blocked["missing_partitions"][0]
    assert missing == {"name": "s3", "status": "withdrawn", "revision": 2}

    # Withdraw s1 too: d1's group breaks, and now every group is blocked.
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-14",
         "revision": 2, "status": "withdrawn"},
    ])
    data = _check(client, "r1", "2026-11-14")
    assert data["ready"] is False
    assert data["available_groups"] == []
    by_group = {g["group_index"]: g for g in data["blocked_groups"]}
    assert {m["name"] for m in by_group[0]["missing_partitions"]} == {"s1"}
    assert {m["name"] for m in by_group[1]["missing_partitions"]} == {"s3"}
    withdrawn = {m["name"]: m for g in data["blocked_groups"]
                 for m in g["missing_partitions"]}
    assert withdrawn["s1"]["revision"] == 2
    assert withdrawn["s3"]["revision"] == 2


def test_republish_at_higher_revision_restores_readiness(client):
    _register_basic(client)
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "d1", "revision": 1,
         "status": "published"},
        {"field_name": "s2", "biz_date": "d1", "revision": 1,
         "status": "published"},
    ])
    _post_partitions(client, [
        {"field_name": "s2", "biz_date": "d1", "revision": 2,
         "status": "withdrawn"},
    ])
    assert _check(client, "r2", "d1")["ready"] is False

    _post_partitions(client, [
        {"field_name": "s2", "biz_date": "d1", "revision": 3,
         "status": "published"},
    ])
    data = _check(client, "r2", "d1")
    assert data["ready"] is True
    assert data["blocked_groups"] == []


def test_dates_are_independent(client):
    _register_basic(client)
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "2026-11-13",
         "revision": 1, "status": "published"},
        {"field_name": "s2", "biz_date": "2026-11-13",
         "revision": 1, "status": "published"},
    ])
    assert _check(client, "r2", "2026-11-13")["ready"] is True
    data = _check(client, "r2", "2026-11-14")
    assert data["ready"] is False
    assert len(data["blocked_groups"][0]["missing_partitions"]) == 2


def test_readiness_target_validation(client):
    _register_basic(client)
    resp = client.post("/readiness/check",
                       json={"report_column": "ghost", "biz_date": "d"})
    assert resp.status_code == 404
    assert resp.json()["details"]["unknown"] == ["ghost"]

    resp = client.post("/readiness/check",
                       json={"report_column": "d1", "biz_date": "d"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_partition_state_survives_lineage_reregistration(client):
    _register_basic(client)
    _post_partitions(client, [
        {"field_name": "s1", "biz_date": "d1", "revision": 1,
         "status": "published"},
        {"field_name": "s2", "biz_date": "d1", "revision": 1,
         "status": "published"},
    ])
    # Re-register r2 with a single-source group.
    client.post("/lineage/register", json={"nodes": [
        {"name": "r2", "kind": "report",
         "candidate_groups": [["s1"]]},
    ]}).raise_for_status()
    data = _check(client, "r2", "d1")
    assert data["ready"] is True
    assert data["available_groups"] == [0]

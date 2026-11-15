"""Functional tests for registration validation and impact analysis."""


def _register(client, nodes, expected=201):
    resp = client.post("/lineage/register", json={"nodes": nodes})
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_simple_retirement_marks_report_failed(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"], ["s2"]]},
    ])
    resp = client.post("/lineage/impact", json={"retiring": ["s1"]})
    assert resp.status_code == 200
    data = resp.json()
    # r1 still has the [s2] candidate group -> must NOT be reported.
    assert data["failed_report_columns"] == []
    assert data["unavailable_nodes"] == ["s1"]

    resp = client.post("/lineage/impact", json={"retiring": ["s1", "s2"]})
    data = resp.json()
    assert len(data["failed_report_columns"]) == 1
    col = data["failed_report_columns"][0]
    assert col["name"] == "r1"
    assert {g["group_index"] for g in col["failed_groups"]} == {0, 1}
    missing_names = {
        m["name"] for g in col["failed_groups"] for m in g["missing"]
    }
    assert missing_names == {"s1", "s2"}


def test_group_requires_all_inputs_available(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1", "s2"]]},
    ])
    # s2 retired: the single group needs both inputs -> column fails.
    data = client.post(
        "/lineage/impact", json={"retiring": ["s2"]}
    ).json()
    assert [c["name"] for c in data["failed_report_columns"]] == ["r1"]
    group = data["failed_report_columns"][0]["failed_groups"][0]
    assert group["inputs"] == ["s1", "s2"]
    assert [m["name"] for m in group["missing"]] == ["s2"]
    assert group["missing"][0]["root_causes"] == ["s2"]


def test_recursive_derived_failure_and_root_causes(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d2", "kind": "derived", "candidate_groups": [["d1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d2"]]},
    ])
    data = client.post(
        "/lineage/impact", json={"retiring": ["s1"]}
    ).json()
    assert data["unavailable_nodes"] == ["d1", "d2", "r1", "s1"]
    col = data["failed_report_columns"][0]
    assert col["name"] == "r1"
    missing = col["failed_groups"][0]["missing"][0]
    assert missing["name"] == "d2"
    # The deepest cause must be the retired source, not the intermediate node.
    assert missing["root_causes"] == ["s1"]


def test_surviving_alternative_group_is_not_false_positive(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "s3", "kind": "source"},
        {"name": "d1", "kind": "derived",
         "candidate_groups": [["s1", "s2"], ["s3"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    # First group of d1 breaks, second ([s3]) keeps d1 and r1 alive.
    data = client.post(
        "/lineage/impact", json={"retiring": ["s1"]}
    ).json()
    assert data["failed_report_columns"] == []
    assert "d1" not in data["unavailable_nodes"]


def test_cycle_detection_rolls_back_whole_batch(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    resp = _register(client, [
        {"name": "d2", "kind": "derived", "candidate_groups": [["d3"]]},
        {"name": "d3", "kind": "derived", "candidate_groups": [["d2"]]},
    ], expected=409)
    assert resp["error"] == "cycle_detected"
    assert resp["details"]["cycle"][0] == resp["details"]["cycle"][-1]

    # Nothing from the rejected batch may be visible.
    graph = client.get("/lineage/graph").json()
    names = {n["name"] for n in graph["nodes"]}
    assert names == {"s1", "d1"}


def test_self_loop_is_rejected(client):
    resp = _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["d1", "s1"]]},
    ], expected=409)
    assert resp["error"] == "cycle_detected"
    assert client.get("/lineage/graph").json()["nodes"] == []


def test_unknown_input_and_other_validation_errors(client):
    # Reference to a node that is not part of the batch or existing graph.
    resp = _register(client, [
        {"name": "d1", "kind": "derived",
         "candidate_groups": [["ghost"]]},
    ], expected=422)
    assert "ghost" in resp["details"]["unknown_inputs"]["d1"]

    # Source nodes cannot declare inputs.
    resp = _register(client, [
        {"name": "s1", "kind": "source",
         "candidate_groups": [["s1"]]},
    ], expected=422)
    assert resp["details"]["source_nodes_with_inputs"] == ["s1"]

    # Non-source nodes must declare at least one group.
    resp = _register(client, [
        {"name": "r1", "kind": "report"},
    ], expected=422)
    assert resp["details"]["missing_candidate_groups"] == ["r1"]

    # Duplicate names inside one batch.
    resp = _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s1", "kind": "source"},
    ], expected=422)
    assert resp["details"]["duplicate_names"] == ["s1"]


def test_kind_conflict_and_upsert_replace_groups(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
    ])
    resp = _register(client, [
        {"name": "r1", "kind": "derived",
         "candidate_groups": [["s1"]]},
    ], expected=409)
    assert resp["details"]["kind_conflicts"] == ["r1"]

    # Re-registration replaces the candidate groups instead of appending.
    _register(client, [
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s2"]]},
    ])
    graph = {n["name"]: n for n in client.get("/lineage/graph").json()["nodes"]}
    assert graph["r1"]["candidate_groups"] == [["s2"]]

    # Retiring s1 must no longer affect r1 after the replacement.
    data = client.post(
        "/lineage/impact", json={"retiring": ["s1"]}
    ).json()
    assert data["failed_report_columns"] == []


def test_unknown_retired_name_returns_404(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    resp = client.post(
        "/lineage/impact", json={"retiring": ["nope"]}
    )
    assert resp.status_code == 404
    assert resp.json()["details"]["unknown"] == ["nope"]


def test_retiring_derived_node_directly(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    data = client.post(
        "/lineage/impact", json={"retiring": ["d1"]}
    ).json()
    assert [c["name"] for c in data["failed_report_columns"]] == ["r1"]
    missing = data["failed_report_columns"][0]["failed_groups"][0]["missing"][0]
    assert missing["name"] == "d1"
    assert missing["root_causes"] == ["d1"]

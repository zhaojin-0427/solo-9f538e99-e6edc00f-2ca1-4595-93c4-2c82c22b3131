"""Concurrency tests for partition readiness against a real uvicorn server.

Guarantees under test:
* a readiness query racing lineage registration and partition updates only
  ever observes one complete committed snapshot (never a half-applied batch);
* revision rules hold under concurrent writers: lower revisions never roll
  state back and same-revision conflicts leave prior state untouched.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

DATE = "2026-11-14"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "readiness_concurrency.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(db_file))
    db.init_db(str(db_file))

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        pytest.fail("uvicorn server did not start")

    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


def _seed(base_url):
    resp = httpx.post(f"{base_url}/lineage/register", json={
        "nodes": [
            {"name": "s1", "kind": "source"},
            {"name": "s2", "kind": "source"},
            {"name": "s3", "kind": "source"},
            {"name": "r1", "kind": "report",
             "candidate_groups": [["s1", "s2"], ["s3"]]},
        ],
    }, timeout=10)
    assert resp.status_code == 201, resp.text


# Two complete lineage snapshots that r1 may switch between atomically.
LINEAGE_A = {"nodes": [
    {"name": "s1", "kind": "source"},
    {"name": "s2", "kind": "source"},
    {"name": "s3", "kind": "source"},
    {"name": "r1", "kind": "report",
     "candidate_groups": [["s1", "s2"], ["s3"]]},
]}
LINEAGE_B = {"nodes": [
    {"name": "s1", "kind": "source"},
    {"name": "s2", "kind": "source"},
    {"name": "s3", "kind": "source"},
    {"name": "r1", "kind": "report",
     "candidate_groups": [["s3"]]},
]}


def test_concurrent_readers_see_only_complete_snapshots(live_server):
    base_url = live_server
    _seed(base_url)
    # Establish revision 2 up front, so concurrent revision-1 writes are
    # always lower than the effective revision and must never be applied.
    r = httpx.post(f"{base_url}/readiness/partitions", json={
        "records": [
            {"field_name": "s1", "biz_date": DATE, "revision": 2,
             "status": "published"},
            {"field_name": "s2", "biz_date": DATE, "revision": 2,
             "status": "published"},
            {"field_name": "s3", "biz_date": DATE, "revision": 2,
             "status": "published"},
        ],
    }, timeout=10)
    assert r.status_code == 200, r.text

    errors: list[Exception] = []
    stop = threading.Event()

    def partition_writer():
        # Drive every source through publish -> withdraw -> republish cycles.
        revision = 3
        while not stop.is_set():
            try:
                status = "published" if revision % 2 else "withdrawn"
                r = httpx.post(f"{base_url}/readiness/partitions", json={
                    "records": [
                        {"field_name": "s1", "biz_date": DATE,
                         "revision": revision, "status": status},
                        {"field_name": "s2", "biz_date": DATE,
                         "revision": revision, "status": status},
                        {"field_name": "s3", "biz_date": DATE,
                         "revision": revision, "status": status},
                    ],
                }, timeout=15)
                assert r.status_code == 200, r.text
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            revision += 1
            time.sleep(0.002)

    def low_revision_writer():
        # Old revisions must always be ignored and never roll state back.
        while not stop.is_set():
            try:
                r = httpx.post(f"{base_url}/readiness/partitions", json={
                    "records": [
                        {"field_name": "s1", "biz_date": DATE,
                         "revision": 1, "status": "withdrawn"},
                        {"field_name": "s3", "biz_date": DATE,
                         "revision": 1, "status": "withdrawn"},
                    ],
                }, timeout=15)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["applied"] == 0
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            time.sleep(0.003)

    def lineage_writer():
        # Atomically swap r1's candidate groups.
        payloads = [LINEAGE_A, LINEAGE_B]
        i = 0
        while not stop.is_set():
            try:
                r = httpx.post(f"{base_url}/lineage/register",
                               json=payloads[i % 2], timeout=15)
                assert r.status_code == 201, r.text
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            i += 1
            time.sleep(0.004)

    def reader_readiness():
        while not stop.is_set():
            try:
                r = httpx.post(f"{base_url}/readiness/check", json={
                    "report_column": "r1", "biz_date": DATE,
                }, timeout=15)
                assert r.status_code == 200, r.text
                data = r.json()
                # Internal consistency of every observed snapshot.
                assert data["ready"] == (bool(data["available_groups"]))
                for group in data["blocked_groups"]:
                    for part in group["missing_partitions"]:
                        assert part["name"] in {"s1", "s2", "s3"}
                        if part["status"] == "withdrawn":
                            assert part["revision"] is not None
                        else:
                            assert part["revision"] is None
                indexes = [g["group_index"] for g in data["blocked_groups"]]
                assert indexes == sorted(indexes)
                assert set(indexes).isdisjoint(data["available_groups"])
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    def reader_graph():
        while not stop.is_set():
            try:
                r = httpx.get(f"{base_url}/lineage/graph", timeout=15)
                assert r.status_code == 200
                r1 = next(n for n in r.json()["nodes"] if n["name"] == "r1")
                # r1's groups must be exactly one complete definition.
                assert r1["candidate_groups"] in (
                    [["s1", "s2"], ["s3"]],
                    [["s3"]],
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    threads = [
        threading.Thread(target=partition_writer, daemon=True),
        threading.Thread(target=low_revision_writer, daemon=True),
        threading.Thread(target=lineage_writer, daemon=True),
        threading.Thread(target=reader_readiness, daemon=True),
        threading.Thread(target=reader_graph, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(4)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, [repr(e) for e in errors[:5]]


def test_conflicting_batch_leaves_partition_state_untouched(live_server):
    base_url = live_server
    _seed(base_url)
    r = httpx.post(f"{base_url}/readiness/partitions", json={
        "records": [
            {"field_name": "s3", "biz_date": DATE,
             "revision": 5, "status": "published"},
        ],
    }, timeout=10)
    assert r.status_code == 200

    # A conflicting batch (same revision, different status) plus an unrelated
    # valid record must roll back as a whole.
    r = httpx.post(f"{base_url}/readiness/partitions", json={
        "records": [
            {"field_name": "s3", "biz_date": DATE,
             "revision": 5, "status": "withdrawn"},
            {"field_name": "s1", "biz_date": DATE,
             "revision": 1, "status": "published"},
        ],
    }, timeout=10)
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"

    data = httpx.post(f"{base_url}/readiness/check", json={
        "report_column": "r1", "biz_date": DATE,
    }, timeout=10).json()
    # s3 rev-5 published is still effective -> alternative group stays usable,
    # and the s1 publish from the rejected batch never landed.
    assert data["ready"] is True
    assert data["available_groups"] == [1]
    blocked = {m["name"]: m for g in data["blocked_groups"]
               for m in g["missing_partitions"]}
    assert blocked["s1"]["status"] == "unpublished"

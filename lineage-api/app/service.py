"""Lineage registration and recursive impact analysis."""

from __future__ import annotations

import itertools
import sqlite3
import sys
from collections import defaultdict

from . import db
from .exceptions import (
    ConflictLineageError,
    CycleDetectedError,
    NotFoundLineageError,
    ValidationLineageError,
)

# Graph traversal may be deep for large lineage graphs.
_MAX_RECURSION_DEPTH = max(10_000, sys.getrecursionlimit())


# --------------------------------------------------------------------------- #
# Graph loading
# --------------------------------------------------------------------------- #

def load_graph(conn: sqlite3.Connection) -> tuple[
    dict[str, str], dict[str, list[list[str]]]
]:
    """Load every node and its candidate input groups from one snapshot."""
    kinds: dict[str, str] = {
        row["name"]: row["kind"] for row in conn.execute("SELECT name, kind FROM nodes")
    }
    groups: dict[str, list[list[str]]] = defaultdict(list)

    group_rows = conn.execute(
        "SELECT id, node_name FROM candidate_groups ORDER BY node_name, position"
    ).fetchall()
    id_to_node = {row["id"]: row["node_name"] for row in group_rows}

    if id_to_node:
        placeholders = ",".join("?" for _ in id_to_node)
        input_rows = conn.execute(
            f"SELECT group_id, input_name FROM group_inputs "
            f"WHERE group_id IN ({placeholders}) ORDER BY group_id, position",
            tuple(id_to_node.keys()),
        ).fetchall()
        inputs_by_group: dict[int, list[str]] = defaultdict(list)
        for row in input_rows:
            inputs_by_group[row["group_id"]].append(row["input_name"])
        for gid, node_name in id_to_node.items():
            groups[node_name].append(inputs_by_group.get(gid, []))

    return kinds, dict(groups)


def adjacency(
    groups: dict[str, list[list[str]]]
) -> dict[str, list[str]]:
    """Flatten candidate groups into node -> unique downstream inputs."""
    edges: dict[str, list[str]] = {}
    for node, node_groups in groups.items():
        seen: set[str] = set()
        ordered: list[str] = []
        for group in node_groups:
            for inp in group:
                if inp not in seen:
                    seen.add(inp)
                    ordered.append(inp)
        edges[node] = ordered
    return edges


# --------------------------------------------------------------------------- #
# Cycle detection (whole graph, DFS coloring)
# --------------------------------------------------------------------------- #

def find_cycle(
    kinds: dict[str, str], groups: dict[str, list[list[str]]]
) -> list[str] | None:
    """Return one cycle path (node repeated at both ends), or None."""
    edges = adjacency(groups)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in kinds}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)
    for name in sorted(kinds):
        if color[name] == WHITE:
            cycle = visit(name)
            if cycle is not None:
                return cycle
    return None


# --------------------------------------------------------------------------- #
# Batch registration (all-or-nothing)
# --------------------------------------------------------------------------- #

def register_batch(
    payload_nodes: list, db_path: str | None = None
) -> dict:
    """Validate and upsert a batch; any failure leaves the graph untouched."""
    # ---- pure payload validation (no lock held yet) ----
    seen_names: set[str] = set()
    duplicates: list[str] = []
    sources_with_inputs: list[str] = []
    empty_group_nodes: list[str] = []
    intra_group_dupes: dict[str, list[list[int]]] = {}

    for node in payload_nodes:
        if node.name in seen_names:
            duplicates.append(node.name)
        seen_names.add(node.name)

        if node.kind == "source" and node.candidate_groups:
            sources_with_inputs.append(node.name)
        if node.kind != "source" and not node.candidate_groups:
            empty_group_nodes.append(node.name)
        for i, group in enumerate(node.candidate_groups):
            if not group:
                empty_group_nodes.append(node.name)
            local_dupes = sorted(
                {inp for inp in group if group.count(inp) > 1}
            )
            if local_dupes:
                intra_group_dupes.setdefault(node.name, []).append(
                    [i, *local_dupes]
                )

    problems: dict[str, list] = {}
    if duplicates:
        problems["duplicate_names"] = sorted(set(duplicates))
    if sources_with_inputs:
        problems["source_nodes_with_inputs"] = sorted(set(sources_with_inputs))
    if empty_group_nodes:
        problems["missing_candidate_groups"] = sorted(set(empty_group_nodes))
    if intra_group_dupes:
        problems["duplicate_inputs_within_group"] = intra_group_dupes
    if problems:
        raise ValidationLineageError(
            "请求数据不合法，整批未登记", problems
        )

    with db.immediate_write(db_path) as conn:
        existing_kinds: dict[str, str] = {
            row["name"]: row["kind"]
            for row in conn.execute("SELECT name, kind FROM nodes")
        }

        # ---- node kind conflicts ----
        conflicts = sorted(
            node.name
            for node in payload_nodes
            if node.name in existing_kinds
            and existing_kinds[node.name] != node.kind
        )
        if conflicts:
            raise ConflictLineageError(
                "节点已存在且类型不同，整批未生效",
                {"kind_conflicts": conflicts},
            )

        # ---- referential integrity against post-batch node set ----
        final_nodes = dict(existing_kinds)
        for node in payload_nodes:
            final_nodes[node.name] = node.kind
        unknown: dict[str, list[str]] = {}
        for node in payload_nodes:
            missing = sorted(
                {
                    inp
                    for group in node.candidate_groups
                    for inp in group
                    if inp not in final_nodes
                }
            )
            if missing:
                unknown[node.name] = missing
        if unknown:
            raise ValidationLineageError(
                "候选输入引用了未登记的字段，整批未生效",
                {"unknown_inputs": unknown},
            )

        # ---- upsert phase 1: all nodes must exist before any edge is written ----
        for node in payload_nodes:
            conn.execute(
                "INSERT INTO nodes(name, kind) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind",
                (node.name, node.kind),
            )

        # ---- upsert phase 2: replace candidate groups and their inputs ----
        for node in payload_nodes:
            # Replace all previously registered groups of this node.
            old_groups = conn.execute(
                "SELECT id FROM candidate_groups WHERE node_name = ?",
                (node.name,),
            ).fetchall()
            for row in old_groups:
                conn.execute(
                    "DELETE FROM group_inputs WHERE group_id = ?", (row["id"],)
                )
            conn.execute(
                "DELETE FROM candidate_groups WHERE node_name = ?", (node.name,)
            )
            for pos, group in enumerate(node.candidate_groups):
                cur = conn.execute(
                    "INSERT INTO candidate_groups(node_name, position) "
                    "VALUES(?, ?)",
                    (node.name, pos),
                )
                gid = cur.lastrowid
                for inp_pos, inp in enumerate(group):
                    conn.execute(
                        "INSERT INTO group_inputs(group_id, input_name, "
                        "position) VALUES(?, ?, ?)",
                        (gid, inp, inp_pos),
                    )

        # ---- whole-graph cycle check; roll back everything on a cycle ----
        kinds, groups = load_graph(conn)
        cycle = find_cycle(kinds, groups)
        if cycle is not None:
            raise CycleDetectedError(
                "登记将引入循环依赖，整批不生效",
                {"cycle": cycle},
            )

    return {
        "registered": len(payload_nodes),
        "nodes": sorted(node.name for node in payload_nodes),
        "message": "登记成功（已提交完整血缘快照）",
    }


# --------------------------------------------------------------------------- #
# Recursive impact analysis
# --------------------------------------------------------------------------- #

def analyze_impact(
    retiring: list[str], db_path: str | None = None
) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)

    unknown = sorted(name for name in retiring if name not in kinds)
    if unknown:
        raise NotFoundLineageError(
            "存在未登记的字段名", {"unknown": unknown}
        )

    retiring_set = set(retiring)
    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)

    # availability memo: True if at least one candidate group is fully usable.
    available: dict[str, bool] = {}
    # For every unavailable node, the deepest retired causes behind it.
    root_causes: dict[str, list[str]] = {}

    def is_available(node: str) -> bool:
        if node in available:
            return available[node]
        if node in retiring_set:
            available[node] = False
            root_causes[node] = [node]
            return False

        node_groups = groups.get(node, [])
        if not node_groups:  # source node not retired
            available[node] = True
            return True

        group_ok = any(
            all(is_available(inp) for inp in group) for group in node_groups
        )
        available[node] = group_ok
        if not group_ok:
            causes: set[str] = set()
            for group in node_groups:
                for inp in group:
                    if not is_available(inp):
                        causes.update(root_causes.get(inp, [inp]))
            root_causes[node] = sorted(causes)
        return group_ok

    for name in kinds:
        is_available(name)

    unavailable = sorted(name for name, ok in available.items() if not ok)

    failed_columns = []
    for name in sorted(kinds):
        if kinds[name] != "report" or available[name]:
            continue
        failed_groups = []
        for idx, group in enumerate(groups.get(name, [])):
            missing = [
                inp for inp in group if not available[inp]
            ]
            if not missing:
                continue
            failed_groups.append(
                {
                    "group_index": idx,
                    "inputs": list(group),
                    "missing": [
                        {
                            "name": inp,
                            "kind": kinds[inp],
                            "root_causes": root_causes.get(inp, [inp]),
                        }
                        for inp in missing
                    ],
                }
            )
        failed_columns.append(
            {"name": name, "failed_groups": failed_groups}
        )

    return {
        "retiring": sorted(retiring_set),
        "failed_report_columns": failed_columns,
        "unavailable_count": len(unavailable),
        "unavailable_nodes": unavailable,
    }


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #

def get_graph(db_path: str | None = None) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)
    nodes = [
        {
            "name": name,
            "kind": kinds[name],
            "candidate_groups": groups.get(name, []),
        }
        for name in sorted(kinds)
    ]
    counts: dict[str, int] = {"source": 0, "derived": 0, "report": 0}
    for kind in kinds.values():
        counts[kind] += 1
    return {"nodes": nodes, "counts": counts}


# --------------------------------------------------------------------------- #
# Partition status recording (batch, revision-aware)
# --------------------------------------------------------------------------- #

def record_partitions(records: list, db_path: str | None = None) -> dict:
    """Record source-field partition states for a batch, all or nothing.

    Revision rules per (field_name, biz_date):
    * only the highest revision is effective; a lower revision retransmission
      is ignored and never rolls the state back;
    * the same revision with a different status conflicts -> whole batch 409;
    * the same revision with the same status is an idempotent no-op.
    Referencing an unregistered node or a non-source field rejects the batch.
    """
    # ---- intra-batch duplicate keys: same status folds into one record,
    #      different status on the same revision is a batch conflict. ----
    folded: dict[tuple[str, str, int], str] = {}
    conflicts: list[dict] = []
    for rec in records:
        key = (rec.field_name, rec.biz_date, rec.revision)
        if key in folded and folded[key] != rec.status:
            conflicts.append(
                {
                    "field_name": key[0],
                    "biz_date": key[1],
                    "revision": key[2],
                    "statuses": sorted({folded[key], rec.status}),
                }
            )
        else:
            folded.setdefault(key, rec.status)
    if conflicts:
        raise ConflictLineageError(
            "同一字段、日期、修订号出现状态冲突，整批未生效",
            {"revision_conflicts": conflicts},
        )

    unique_records = [
        (field_name, biz_date, revision, status)
        for (field_name, biz_date, revision), status in folded.items()
    ]

    with db.immediate_write(db_path) as conn:
        existing_kinds: dict[str, str] = {
            row["name"]: row["kind"]
            for row in conn.execute("SELECT name, kind FROM nodes")
        }

        # ---- referential / kind validation: source fields only ----
        unknown = sorted(
            {field_name for field_name, _, _, _ in unique_records
             if field_name not in existing_kinds}
        )
        non_source = sorted(
            {field_name for field_name, _, _, _ in unique_records
             if existing_kinds.get(field_name) not in (None, "source")}
        )
        problems: dict[str, list] = {}
        if unknown:
            problems["unregistered_fields"] = unknown
        if non_source:
            problems["non_source_fields"] = non_source
        if problems:
            raise ValidationLineageError(
                "引用了未登记字段或非源字段，整批未生效", problems
            )

        # ---- decide the outcome of every record before mutating anything ----
        tuple_placeholders = ",".join("(?,?,?)" for _ in unique_records)
        keys = [(f, d, r) for f, d, r, _ in unique_records]
        stored_rows = conn.execute(
            f"SELECT field_name, biz_date, revision, status "
            f"FROM partition_status "
            f"WHERE (field_name, biz_date, revision) IN "
            f"({tuple_placeholders})",
            tuple(itertools.chain.from_iterable(keys)),
        ).fetchall()
        stored = {
            (row["field_name"], row["biz_date"], row["revision"]):
                row["status"]
            for row in stored_rows
        }

        # Only (field, date) pairs touched by this batch can be affected.
        pair_placeholders = ",".join("(?,?)" for _ in unique_records)
        pairs = [(f, d) for f, d, _, _ in unique_records]
        max_revision: dict[tuple[str, str], int] = {}
        if pairs:
            for row in conn.execute(
                f"SELECT field_name, biz_date, MAX(revision) AS max_rev "
                f"FROM partition_status "
                f"WHERE (field_name, biz_date) IN ({pair_placeholders}) "
                f"GROUP BY field_name, biz_date",
                tuple(itertools.chain.from_iterable(pairs)),
            ):
                max_revision[(row["field_name"], row["biz_date"])] = (
                    row["max_rev"]
                )

        plans: list[tuple[str, str, int, str, str]] = []
        stored_conflicts: list[dict] = []
        for field_name, biz_date, revision, status in unique_records:
            key = (field_name, biz_date, revision)
            if key in stored:
                if stored[key] != status:
                    stored_conflicts.append(
                        {
                            "field_name": field_name,
                            "biz_date": biz_date,
                            "revision": revision,
                            "stored_status": stored[key],
                            "requested_status": status,
                        }
                    )
                else:
                    plans.append((field_name, biz_date, revision,
                                  status, "unchanged"))
                continue
            if revision < max_revision.get((field_name, biz_date), 0):
                # Lower revision retransmission: never rolls back.
                plans.append((field_name, biz_date, revision,
                              status, "superseded"))
            else:
                plans.append((field_name, biz_date, revision,
                              status, "applied"))
        if stored_conflicts:
            raise ConflictLineageError(
                "同一字段、日期、修订号已存在不同状态，整批未生效",
                {"revision_conflicts": stored_conflicts},
            )

        # ---- mutation phase: only genuine new revisions are inserted ----
        applied = 0
        for field_name, biz_date, revision, status, outcome in plans:
            if outcome == "applied":
                conn.execute(
                    "INSERT INTO partition_status"
                    "(field_name, biz_date, revision, status) "
                    "VALUES(?, ?, ?, ?)",
                    (field_name, biz_date, revision, status),
                )
                applied += 1

    unchanged = sum(1 for *_, o in plans if o == "unchanged")
    superseded = sum(1 for *_, o in plans if o == "superseded")
    results = [
        {
            "field_name": field_name,
            "biz_date": biz_date,
            "revision": revision,
            "status": status,
            "outcome": outcome,
        }
        for field_name, biz_date, revision, status, outcome in plans
    ]
    results.sort(key=lambda item: (item["field_name"], item["biz_date"],
                                   item["revision"]))
    return {
        "received": len(records),
        "applied": applied,
        "unchanged": unchanged,
        "superseded": superseded,
        "rejected": 0,
        "results": results,
        "message": "分区状态批次已提交（整批原子）",
    }


# --------------------------------------------------------------------------- #
# Readiness check against candidate input groups
# --------------------------------------------------------------------------- #

def _load_partitions(
    conn: sqlite3.Connection, biz_date: str
) -> dict[str, sqlite3.Row]:
    """Effective (highest-revision) partition row of each source on the date."""
    rows = conn.execute(
        "SELECT field_name, revision, status FROM partition_status "
        "WHERE biz_date = ? AND revision = ("
        "    SELECT MAX(revision) FROM partition_status p2 "
        "    WHERE p2.field_name = partition_status.field_name "
        "      AND p2.biz_date = partition_status.biz_date"
        ")",
        (biz_date,),
    ).fetchall()
    return {row["field_name"]: row for row in rows}


def check_readiness(
    report_column: str, biz_date: str, db_path: str | None = None
) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)
        partitions = _load_partitions(conn, biz_date)

    if report_column not in kinds:
        raise NotFoundLineageError(
            "报表列未登记", {"unknown": [report_column]}
        )
    if kinds[report_column] != "report":
        raise ValidationLineageError(
            "就绪核查目标必须是 report 类型节点",
            {"report_column": report_column, "kind": kinds[report_column]},
        )

    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)

    # Availability memo over the lineage graph for this one business date.
    available: dict[str, bool] = {}
    # For each unavailable node: source-field names whose partition is missing.
    root_missing: dict[str, list[str]] = {}

    def is_available(node: str) -> bool:
        if node in available:
            return available[node]

        if kinds[node] == "source":
            row = partitions.get(node)
            ok = row is not None and row["status"] == "published"
            available[node] = ok
            if not ok:
                root_missing[node] = [node]
            return ok

        node_groups = groups.get(node, [])
        group_ok = any(
            all(is_available(inp) for inp in group) for group in node_groups
        )
        available[node] = group_ok
        if not group_ok:
            causes: set[str] = set()
            for group in node_groups:
                for inp in group:
                    if not is_available(inp):
                        causes.update(root_missing.get(inp, [inp]))
            root_missing[node] = sorted(causes)
        return group_ok

    column_groups = groups.get(report_column, [])
    available_groups: list[int] = []
    blocked_groups: list[dict] = []
    for idx, group in enumerate(column_groups):
        if all(is_available(inp) for inp in group):
            available_groups.append(idx)
            continue
        missing_names = sorted({
            source
            for inp in group
            if not is_available(inp)
            for source in root_missing.get(inp, [inp])
        })
        blocked_groups.append(
            {
                "group_index": idx,
                "inputs": list(group),
                "missing_partitions": [
                    _missing_partition_view(name, partitions)
                    for name in missing_names
                ],
            }
        )

    return {
        "report_column": report_column,
        "biz_date": biz_date,
        "ready": bool(available_groups),
        "available_groups": available_groups,
        "blocked_groups": blocked_groups,
    }


def _missing_partition_view(
    source_name: str, partitions: dict
) -> dict:
    row = partitions.get(source_name)
    if row is None:
        # No record at all -> treated as unpublished.
        return {"name": source_name, "status": "unpublished", "revision": None}
    return {
        "name": source_name,
        "status": row["status"],  # effective state must be withdrawn here
        "revision": row["revision"],
    }

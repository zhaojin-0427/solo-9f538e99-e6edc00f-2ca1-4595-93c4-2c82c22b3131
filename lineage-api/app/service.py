"""Lineage registration and recursive impact analysis."""

from __future__ import annotations

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

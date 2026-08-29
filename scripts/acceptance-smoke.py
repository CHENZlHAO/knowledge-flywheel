#!/usr/bin/env python3
"""Run the host-side Docker Compose acceptance flow against a live Hub."""

from __future__ import annotations

import json
import hashlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Union
from uuid import uuid4


JsonValue = Union[dict, list]


def request(base: str, method: str, path: str, payload: Optional[dict] = None) -> JsonValue:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def wait_for_task(base: str, task_id: int, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = request(base, "GET", "/api/v1/tasks")
        task = next(item for item in tasks if item["id"] == task_id)
        if task["status"] in {"success", "failed"}:
            return task
        time.sleep(0.25)
    raise RuntimeError(f"task {task_id} did not finish within {timeout:g}s")


def wait_for_task_status(base: str, task_id: int, statuses: set[str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = request(base, "GET", "/api/v1/tasks")
        task = next(item for item in tasks if item["id"] == task_id)
        if task["status"] in statuses:
            return task
        time.sleep(0.25)
    raise RuntimeError(f"task {task_id} did not reach {sorted(statuses)} within {timeout:g}s")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    health = request(base, "GET", "/healthz")
    if health.get("status") != "ok":
        raise RuntimeError(f"Hub is not healthy: {health}")

    node_id = f"acceptance-{uuid4()}"
    file_path = f"acceptance/{node_id}.txt"
    request(
        base,
        "POST",
        "/api/v1/nodes/heartbeat",
        {
            "node_id": node_id,
            "hostname": "acceptance-pc",
            "agent_version": "acceptance",
            "cpu_percent": 1,
            "disk_free_bytes": 1024,
        },
    )
    request(
        base,
        "POST",
        "/api/v1/files/report",
        {"node_id": node_id, "path": file_path, "file_hash": hashlib.sha256(b"acceptance content").hexdigest(), "size_bytes": len(b"acceptance content")},
    )
    deadline = time.monotonic() + 15
    pipeline_file = None
    while time.monotonic() < deadline:
        pipeline = request(base, "GET", "/api/v1/pipeline/files")
        pipeline_file = next(item for item in pipeline if item["source_node_id"] == node_id)
        if pipeline_file["status"] in {"registered", "parsed"} and pipeline_file["register_task_status"] == "success":
            break
        time.sleep(0.25)
    if not pipeline_file or pipeline_file["status"] not in {"registered", "parsed"} or pipeline_file["register_task_status"] != "success":
        raise RuntimeError(f"file pipeline did not register: {pipeline_file}")

    content = "acceptance content"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    parse = request(
        base,
        "POST",
        f"/api/v1/files/{pipeline_file['id']}/content",
        {"source_node_id": node_id, "file_hash": content_hash, "content": content},
    )
    deadline = time.monotonic() + 15
    parsed_file = None
    while time.monotonic() < deadline:
        pipeline = request(base, "GET", "/api/v1/pipeline/files")
        parsed_file = next(item for item in pipeline if item["id"] == pipeline_file["id"])
        if parsed_file["status"] == "parsed" and parsed_file["parse_task_status"] == "success" and parsed_file["chunk_count"] > 0:
            break
        time.sleep(0.25)
    if not parsed_file or parsed_file["status"] != "parsed" or parsed_file["parse_task_status"] != "success" or parsed_file["chunk_count"] <= 0:
        raise RuntimeError(f"file pipeline did not parse: {parsed_file}")
    deadline = time.monotonic() + 20
    embedded_file = None
    while time.monotonic() < deadline:
        pipeline = request(base, "GET", "/api/v1/pipeline/files")
        embedded_file = next(item for item in pipeline if item["id"] == pipeline_file["id"])
        if embedded_file.get("embed_task_status") == "success" and embedded_file.get("embedded_chunk_count") == embedded_file.get("chunk_count"):
            break
        time.sleep(0.25)
    if not embedded_file or embedded_file.get("embed_task_status") != "success":
        raise RuntimeError(f"file pipeline did not embed or degrade safely: {embedded_file}")
    search = request(base, "POST", "/api/v1/knowledge/search", {"query": "acceptance content", "top_k": 5, "idempotency_key": f"acceptance-search-{uuid4()}"})
    if search.get("count", 0) < 1 or not search.get("embedding_status"):
        raise RuntimeError(f"knowledge search contract changed: {search}")
    reconciliation = request(base, "POST", "/api/v1/reconciliation/files")
    if reconciliation["checked"] < 1 or reconciliation["marked_missing"] != 0:
        raise RuntimeError(f"unexpected liveness reconciliation result: {reconciliation}")

    noop = request(
        base,
        "POST",
        "/api/v1/tasks",
        {"kind": "noop", "idempotency_key": f"acceptance-noop-{uuid4()}", "payload": {"source": "smoke"}},
    )
    noop_done = wait_for_task(base, noop["id"])
    if noop_done["status"] != "success":
        raise RuntimeError(f"noop task did not succeed: {noop_done}")

    unsupported = request(
        base,
        "POST",
        "/api/v1/tasks",
        {"kind": "parse", "idempotency_key": f"acceptance-parse-{uuid4()}", "payload": {}},
    )
    unsupported_done = wait_for_task(base, unsupported["id"])
    if unsupported_done["status"] != "failed" or "executor not installed" not in (unsupported_done["error"] or ""):
        raise RuntimeError(f"unsupported task failure contract changed: {unsupported_done}")

    command = request(
        base,
        "POST",
        "/api/v1/mobile/commands",
        {
            "node_id": node_id,
            "command_type": "retry_task",
            "idempotency_key": f"acceptance-command-{uuid4()}",
            "payload": {"task_id": unsupported["id"]},
            "requested_by": "acceptance-smoke",
        },
    )
    claimed = request(base, "GET", f"/api/v1/nodes/{node_id}/commands/next")["command"]
    if claimed is None or claimed["id"] != command["id"] or claimed["status"] != "running":
        raise RuntimeError(f"command claim contract changed: {claimed}")
    request(
        base,
        "POST",
        f"/api/v1/nodes/{node_id}/commands/{command['id']}/ack",
        {"status": "success", "result": {"execution": "smoke"}},
    )
    final_command = request(base, "GET", f"/api/v1/mobile/commands/{command['id']}")
    if final_command["status"] != "success":
        raise RuntimeError(f"command did not complete: {final_command}")

    replica_node_id = f"replica-{uuid4()}"
    request(
        base,
        "POST",
        "/api/v1/nodes/heartbeat",
        {
            "node_id": replica_node_id,
            "hostname": "acceptance-replica",
            "agent_version": "acceptance",
            "cpu_percent": 1,
            "disk_free_bytes": 1024,
            "is_replica": True,
        },
    )
    repair = request(
        base,
        "POST",
        "/api/v1/tasks",
        {
            "kind": "replica_repair",
            "idempotency_key": f"acceptance-repair-{uuid4()}",
            "payload": {"file_id": pipeline_file["id"], "file_hash": content_hash, "target_node_id": replica_node_id},
        },
    )
    dispatched = wait_for_task_status(base, repair["id"], {"waiting", "failed"})
    if dispatched["status"] != "waiting":
        raise RuntimeError(f"replica repair was not dispatched: {dispatched}")
    sync_command = request(base, "GET", f"/api/v1/nodes/{replica_node_id}/commands/next")["command"]
    if sync_command is None or sync_command["command_type"] != "sync_replica":
        raise RuntimeError(f"replica sync command was not claimable: {sync_command}")
    request(
        base,
        "POST",
        f"/api/v1/nodes/{replica_node_id}/commands/{sync_command['id']}/ack",
        {"status": "success", "result": {"execution": "acceptance_verified", "verified": True, "file_hash": content_hash}},
    )
    repair_done = wait_for_task(base, repair["id"])
    if repair_done["status"] != "success":
        raise RuntimeError(f"verified replica repair did not complete: {repair_done}")
    replicas = request(base, "GET", f"/api/v1/files/{pipeline_file['id']}/replicas")
    if not any(item["node_id"] == replica_node_id and item["status"] == "healthy" for item in replicas):
        raise RuntimeError(f"verified replica was not recorded: {replicas}")

    flywheel_query = f"验收知识缺口 {node_id}"
    flywheel_key = f"acceptance-flywheel-{uuid4()}"
    retrieval_payload = {"idempotency_key": flywheel_key, "query": flywheel_query, "result_count": 0, "actor": "acceptance-smoke"}
    first_event = request(base, "POST", "/api/v1/flywheel/retrievals", retrieval_payload)
    duplicate_event = request(base, "POST", "/api/v1/flywheel/retrievals", retrieval_payload)
    if duplicate_event.get("id") != first_event.get("id"):
        raise RuntimeError(f"flywheel idempotency contract changed: {first_event}, {duplicate_event}")
    request(base, "POST", "/api/v1/flywheel/feedback", {"idempotency_key": f"{flywheel_key}-feedback", "query": f"  {flywheel_query}  ", "rating": 1, "actor": "acceptance-smoke"})
    gaps = request(base, "GET", "/api/v1/flywheel/gaps")
    normalized = "".join(flywheel_query.casefold().split())
    gap = next((item for item in gaps if item.get("normalized_query") == normalized), None)
    if not gap or gap["no_result_count"] != 1 or gap["negative_feedback_count"] != 1 or gap["score"] != 3:
        raise RuntimeError(f"flywheel gap aggregation contract changed: {gap}")
    proposal = request(base, "POST", f"/api/v1/flywheel/proposals?query={urllib.parse.quote(normalized)}")
    if proposal.get("status") != "pending" or "human_review_required" not in proposal.get("body", ""):
        raise RuntimeError(f"flywheel proposal governance contract changed: {proposal}")
    mobile_gaps = request(base, "GET", "/api/v1/mobile/flywheel/gaps")
    if not any(item.get("normalized_query") == normalized for item in mobile_gaps):
        raise RuntimeError(f"mobile flywheel read contract changed: {mobile_gaps}")

    print(json.dumps({"status": "passed", "node_id": node_id, "registered_file": pipeline_file["id"], "parse_task": parse["task_id"], "chunks": parsed_file["chunk_count"], "embedding_status": embedded_file.get("embedding_status"), "search_results": search.get("count"), "reconciliation_checked": reconciliation["checked"], "noop_task": noop_done["id"], "unsupported_task": unsupported_done["id"], "command": command["id"], "replica_repair": repair_done["id"], "replica_command": sync_command["id"], "flywheel_proposal": proposal["id"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"acceptance-smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

import json
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import DocumentChunk, FileRecord, FileReplica, Node, RemoteCommand, Task
from app.services import acknowledge_remote_command, reconcile_file_liveness, report_file, run_task_by_id, run_task_once
from app.schemas import FileReport
from app.schemas import MobileCommandAck


def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_worker_executes_deterministic_noop_task():
    Session = session_factory()
    with Session() as db:
        task = Task(idempotency_key="worker-noop", kind="noop", payload_json=json.dumps({"source": "test"}))
        db.add(task)
        db.commit()
        task_id = task.id
        result = run_task_once(db, "test-worker", lease_seconds=60)
        assert result is not None
        assert result.id == task_id
        assert result.status == "success"
        assert json.loads(result.result_json)["execution"] == "noop"
        assert result.claimed_at is None


def test_targeted_delivery_is_idempotent_after_success():
    Session = session_factory()
    with Session() as db:
        task = Task(idempotency_key="celery-noop", kind="noop", payload_json="{}")
        db.add(task)
        db.commit()
        first = run_task_by_id(db, task.id, "celery:first", lease_seconds=60)
        second = run_task_by_id(db, task.id, "celery:duplicate", lease_seconds=60)
        assert first is not None
        assert first.status == "success"
        assert second is None
        assert db.get(Task, task.id).attempts == 1


def test_targeted_delivery_does_not_steal_active_lease():
    Session = session_factory()
    with Session() as db:
        task = Task(
            idempotency_key="celery-active-lease",
            kind="noop",
            payload_json="{}",
            status="running",
            claimed_at=datetime.now(timezone.utc),
            claimed_by="poll-worker",
        )
        db.add(task)
        db.commit()
        assert run_task_by_id(db, task.id, "celery:duplicate", lease_seconds=60) is None
        db.refresh(task)
        assert task.status == "running"
        assert task.claimed_by == "poll-worker"


def test_worker_requeues_expired_lease_and_records_failure():
    Session = session_factory()
    with Session() as db:
        task = Task(
            idempotency_key="worker-unsupported",
            kind="parse",
            payload_json="{}",
            status="running",
            claimed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            claimed_by="dead-worker",
        )
        db.add(task)
        db.commit()
        result = run_task_once(db, "recovery-worker", lease_seconds=60)
        assert result is not None
        assert result.status == "failed"
        assert "executor not installed" in result.error
        assert result.claimed_by is None


def test_worker_registers_reported_file_and_preserves_hash_lineage():
    Session = session_factory()
    with Session() as db:
        record = FileRecord(path="docs/a.md", file_hash="a" * 64, size_bytes=12, source_node_id="node-1")
        db.add(record)
        db.flush()
        task = Task(
            idempotency_key=f"file-register:{record.id}:{record.file_hash}",
            kind="file_register",
            payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash}),
        )
        db.add(task)
        db.commit()
        result = run_task_once(db, "file-worker", lease_seconds=60)
        assert result.status == "success"
        db.refresh(record)
        assert record.status == "registered"
        assert json.loads(result.result_json)["file_hash"] == "a" * 64


def test_repeated_registered_report_does_not_regress_file_status():
    Session = session_factory()
    with Session() as db:
        data = FileReport(node_id="node-1", path="docs/a.md", file_hash="b" * 64, size_bytes=12)
        record = report_file(db, data)
        run_task_once(db, "file-worker", lease_seconds=60)
        db.refresh(record)
        assert record.status == "registered"
        report_file(db, data)
        db.refresh(record)
        assert record.status == "registered"
        assert db.query(Task).filter_by(kind="file_register").count() == 1


def test_worker_parses_text_into_deterministic_chunks():
    Session = session_factory()
    content = ("第一段内容。\n\n" + "第二段内容。" * 4).strip()
    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with Session() as db:
        record = FileRecord(path="docs/parse.md", file_hash=file_hash, size_bytes=len(content.encode()), source_node_id="node-parse")
        db.add(record)
        db.flush()
        task = Task(
            idempotency_key=f"file-parse:{record.id}:{file_hash}",
            kind="file_parse",
            payload_json=json.dumps({"file_id": record.id, "file_hash": file_hash, "content": content}),
        )
        db.add(task)
        db.commit()
        result = run_task_once(db, "parse-worker", lease_seconds=60)
        assert result.status == "success"
        db.refresh(record)
        chunks = db.query(DocumentChunk).filter_by(file_id=record.id).order_by(DocumentChunk.chunk_index).all()
        assert record.status == "parsed"
        assert len(chunks) == 1
        assert chunks[0].content == content
        assert chunks[0].content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_worker_rejects_parse_hash_lineage_mismatch():
    Session = session_factory()
    content = "可信内容"
    with Session() as db:
        record = FileRecord(path="docs/mismatch.md", file_hash="a" * 64, size_bytes=12, source_node_id="node-parse")
        db.add(record)
        db.flush()
        task = Task(
            idempotency_key=f"file-parse:{record.id}:wrong",
            kind="file_parse",
            payload_json=json.dumps({"file_id": record.id, "file_hash": "b" * 64, "content": content}),
        )
        db.add(task)
        db.commit()
        result = run_task_once(db, "parse-worker", lease_seconds=60)
        db.refresh(record)
        assert result.status == "failed"
        assert "lineage mismatch" in result.error
        assert record.status == "reported"


def test_file_liveness_reconciliation_marks_missing_and_report_recovers():
    Session = session_factory()
    with Session() as db:
        record = FileRecord(path="docs/stale.md", file_hash="d" * 64, size_bytes=10, source_node_id="node-stale")
        db.add(record)
        db.commit()
        record.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        result = reconcile_file_liveness(db, missing_after=60)
        db.refresh(record)
        assert result["marked_missing"] == 1
        assert record.alive is False
        assert record.status == "missing"
        report_file(db, FileReport(node_id="node-stale", path="docs/stale.md", file_hash="d" * 64, size_bytes=10))
        db.refresh(record)
        assert record.alive is True
        assert record.status == "reported"


def test_reconciliation_queues_replica_repair_for_missing_target():
    Session = session_factory()
    with Session() as db:
        db.add(Node(id="source", hostname="source"))
        db.add(Node(id="replica-a", hostname="replica-a", is_replica=True))
        record = FileRecord(path="docs/repair.md", file_hash="1" * 64, size_bytes=4, source_node_id="source")
        db.add(record)
        db.flush()
        db.add(FileReplica(file_id=record.id, node_id="source", file_hash=record.file_hash, status="healthy"))
        db.commit()
        result = reconcile_file_liveness(db, missing_after=60, replica_node_ids=["replica-a"])
        task = db.query(Task).filter_by(kind="replica_repair").one()
        assert result["repair_queued"] == 1
        assert task.status == "pending"


def test_reconciliation_rejects_unregistered_or_non_replica_targets():
    Session = session_factory()
    with Session() as db:
        db.add(Node(id="source", hostname="source"))
        db.add(Node(id="ordinary", hostname="ordinary", is_replica=False))
        record = FileRecord(path="docs/policy.md", file_hash="3" * 64, size_bytes=4, source_node_id="source")
        db.add(record)
        db.flush()
        db.add(FileReplica(file_id=record.id, node_id="source", file_hash=record.file_hash, status="healthy"))
        db.commit()

        result = reconcile_file_liveness(db, missing_after=60, replica_node_ids=["missing-node", "ordinary"])

        assert result["invalid_replica_nodes"] == ["missing-node", "ordinary"]
        assert result["repair_queued"] == 0
        assert db.query(Task).filter_by(kind="replica_repair").count() == 0


def test_replica_repair_dispatches_command_and_waits_for_verified_ack():
    Session = session_factory()
    with Session() as db:
        db.add(Node(id="source", hostname="source"))
        db.add(Node(id="replica-a", hostname="replica-a", is_replica=True))
        record = FileRecord(path="docs/dispatch.md", file_hash="4" * 64, size_bytes=4, source_node_id="source")
        db.add(record)
        db.flush()
        db.add(FileReplica(file_id=record.id, node_id="source", file_hash=record.file_hash, status="healthy"))
        task = Task(
            idempotency_key=f"replica-repair:{record.id}:{record.file_hash}:replica-a",
            kind="replica_repair",
            payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash, "target_node_id": "replica-a"}),
        )
        db.add(task)
        db.commit()

        result = run_task_once(db, "replica-worker", lease_seconds=60)

        assert result.status == "waiting"
        command = db.query(RemoteCommand).filter_by(command_type="sync_replica").one()
        assert command.node_id == "replica-a"
        assert json.loads(command.payload_json)["relative_path"] == "docs/dispatch.md"

        acknowledge_remote_command(
            db,
            command,
            MobileCommandAck(status="success", result={"verified": True, "file_hash": record.file_hash}),
        )
        db.refresh(result)
        replica = db.query(FileReplica).filter_by(file_id=record.id, node_id="replica-a").one()
        assert result.status == "success"
        assert replica.status == "healthy"


def test_replica_repair_failure_ack_fails_task_without_creating_replica():
    Session = session_factory()
    with Session() as db:
        db.add(Node(id="source", hostname="source"))
        db.add(Node(id="replica-a", hostname="replica-a", is_replica=True))
        record = FileRecord(path="docs/fail.md", file_hash="5" * 64, size_bytes=4, source_node_id="source")
        db.add(record)
        db.flush()
        task = Task(
            idempotency_key=f"replica-repair:{record.id}:{record.file_hash}:replica-a",
            kind="replica_repair",
            payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash, "target_node_id": "replica-a"}),
        )
        db.add(task)
        db.commit()
        result = run_task_once(db, "replica-worker", lease_seconds=60)
        command = db.query(RemoteCommand).filter_by(command_type="sync_replica").one()

        acknowledge_remote_command(
            db,
            command,
            MobileCommandAck(status="failed", result={"execution": "adapter_missing"}, error="sync adapter not installed"),
        )

        db.refresh(result)
        assert result.status == "failed"
        assert "sync adapter not installed" in result.error
        assert db.query(FileReplica).filter_by(file_id=record.id, node_id="replica-a").count() == 0

        result.status = "pending"
        result.error = None
        db.commit()
        retried = run_task_once(db, "replica-worker", lease_seconds=60)
        db.refresh(command)
        assert retried.status == "waiting"
        assert command.status == "queued"


def test_unverified_success_ack_is_persisted_as_failure():
    Session = session_factory()
    with Session() as db:
        db.add(Node(id="replica-a", hostname="replica-a", is_replica=True))
        record = FileRecord(path="docs/unverified.md", file_hash="6" * 64, size_bytes=4, source_node_id="source")
        db.add(record)
        db.flush()
        task = Task(idempotency_key="unverified-repair", kind="replica_repair", status="waiting", payload_json="{}")
        db.add(task)
        db.flush()
        command = RemoteCommand(
            idempotency_key="unverified-command",
            node_id="replica-a",
            command_type="sync_replica",
            status="running",
            requested_by="worker",
            payload_json=json.dumps({"repair_task_id": task.id, "file_id": record.id, "file_hash": record.file_hash}),
        )
        db.add(command)
        db.commit()

        acknowledge_remote_command(db, command, MobileCommandAck(status="success", result={"verified": False, "file_hash": record.file_hash}))

        assert command.status == "failed"
        assert task.status == "failed"
        assert "verified=true" in command.error


def test_missing_parsed_file_recovers_to_parsed_stage():
    Session = session_factory()
    with Session() as db:
        data = FileReport(node_id="node-recovered", path="docs/recovered.md", file_hash="2" * 64, size_bytes=8)
        record = report_file(db, data)
        register_task = db.query(Task).filter_by(kind="file_register").one()
        register_task.status = "success"
        parse_task = Task(
            idempotency_key=f"file-parse:{record.id}:{record.file_hash}",
            kind="file_parse",
            status="success",
            payload_json="{}",
        )
        record.status = "missing"
        record.alive = False
        db.add(parse_task)
        db.commit()
        report_file(db, data)
        db.refresh(record)
        assert record.status == "parsed"
        assert record.alive is True

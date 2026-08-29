import os
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import pytest

import app.alerting as alerting
from app.config import settings
from app.db import SessionLocal
from app.models import Alert, AlertDelivery, AuditLog


@pytest.fixture()
def enabled_webhook(monkeypatch):
    monkeypatch.setattr(settings, "alert_channels", "webhook")
    monkeypatch.setattr(settings, "alert_webhook_url", "http://127.0.0.1:9/hook")
    monkeypatch.setattr(settings, "alert_retry_max", 2)
    monkeypatch.setattr(settings, "alert_retry_backoff_seconds", 0)
    monkeypatch.setattr(settings, "alert_suppress_window_seconds", 300)


def _make_alert() -> int:
    with SessionLocal() as db:
        alert = Alert(fingerprint=f"test:{os.urandom(6).hex()}", severity="high", kind="test_alert", resource_type="test", resource_id="1", message="test message")
        db.add(alert)
        db.commit()
        db.refresh(alert)
        return alert.id


def _reset_deliveries():
    with SessionLocal() as db:
        db.query(AlertDelivery).delete(synchronize_session=False)
        db.query(AuditLog).delete(synchronize_session=False)
        db.commit()


def test_ensure_deliveries_creates_pending_rows(enabled_webhook):
    _reset_deliveries()
    alert_id = _make_alert()
    with SessionLocal() as db:
        alert = db.get(Alert, alert_id)
        created = alerting.ensure_deliveries(db, alert)
        db.commit()
    assert len(created) == 1
    with SessionLocal() as db:
        rows = db.query(AlertDelivery).filter(AlertDelivery.alert_id == alert_id).all()
        assert [row.status for row in rows] == ["pending"]


def test_delivery_success_marks_sent(enabled_webhook, monkeypatch):
    _reset_deliveries()
    sent = []
    class FakeNotifier:
        channel = "webhook"
        def send(self, alert):
            sent.append(alert.id)
    monkeypatch.setattr(alerting, "build_notifiers", lambda: [FakeNotifier()])
    alert_id = _make_alert()
    with SessionLocal() as db:
        alerting.ensure_deliveries(db, db.get(Alert, alert_id))
        db.commit()
        counts = alerting.process_alert_deliveries(db)
    assert counts["sent"] == 1
    assert sent == [alert_id]
    with SessionLocal() as db:
        assert db.query(AlertDelivery).filter(AlertDelivery.alert_id == alert_id).one().status == "sent"


def test_delivery_failure_retries_then_dead_letters(enabled_webhook, monkeypatch):
    _reset_deliveries()
    class FailingNotifier:
        channel = "webhook"
        def send(self, alert):
            raise RuntimeError("boom")
    monkeypatch.setattr(alerting, "build_notifiers", lambda: [FailingNotifier()])
    alert_id = _make_alert()
    with SessionLocal() as db:
        alerting.ensure_deliveries(db, db.get(Alert, alert_id))
        db.commit()
        first = alerting.process_alert_deliveries(db)
    assert first["failed"] == 1
    with SessionLocal() as db:
        row = db.query(AlertDelivery).filter(AlertDelivery.alert_id == alert_id).one()
        assert row.attempts == 1
        # Simulate the backoff window elapsing.
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.commit()
        second = alerting.process_alert_deliveries(db)
    assert second["dead_letter"] == 1
    with SessionLocal() as db:
        assert db.query(AlertDelivery).filter(AlertDelivery.alert_id == alert_id).one().status == "dead_letter"


def test_realert_within_window_is_suppressed(enabled_webhook, monkeypatch):
    _reset_deliveries()
    calls = []
    class FakeNotifier:
        channel = "webhook"
        def send(self, alert):
            calls.append(alert.id)
    monkeypatch.setattr(alerting, "build_notifiers", lambda: [FakeNotifier()])
    alert_id = _make_alert()
    with SessionLocal() as db:
        alert = db.get(Alert, alert_id)
        alerting.ensure_deliveries(db, alert)
        db.commit()
        alerting.process_alert_deliveries(db)
        # Re-fire the same alert within the suppression window.
        alerting.ensure_deliveries(db, alert)
        db.commit()
    assert len(calls) == 1  # no duplicate send
    with SessionLocal() as db:
        suppressed = db.query(AuditLog).filter(AuditLog.action == "alert_delivery.suppressed").count()
        assert suppressed == 1

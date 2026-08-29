"""External alert delivery: email, generic webhook, WeCom, DingTalk.

Delivery is persisted first (``AlertDelivery``) and then attempted. The database
row is the durable record; failed attempts are retried with backoff and finally
land in ``dead_letter``. Repeated alerts for the same alert/channel are
suppressed for a configurable window. Every notifier raises on failure so the
shared retry state machine can mark attempts/errors consistently.
"""
from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Protocol
from urllib.request import Request, urlopen

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Alert, AlertDelivery, AuditLog


class Notifier(Protocol):
    channel: str

    def send(self, alert: Alert) -> None: ...


def _post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> None:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        response.read()


class WebhookNotifier:
    channel = "webhook"

    def __init__(self, url: str):
        self.url = url

    def send(self, alert: Alert) -> None:
        _post_json(self.url, {
            "alert_id": alert.id,
            "severity": alert.severity,
            "kind": alert.kind,
            "resource_type": alert.resource_type,
            "resource_id": alert.resource_id,
            "message": alert.message,
            "status": alert.status,
        })


class WeComNotifier:
    channel = "wecom"

    def __init__(self, url: str):
        self.url = url

    def send(self, alert: Alert) -> None:
        _post_json(self.url, {
            "msgtype": "text",
            "text": {"content": f"[{alert.severity}] {alert.kind}: {alert.message}"},
        })


class DingTalkNotifier:
    channel = "dingtalk"

    def __init__(self, url: str):
        self.url = url

    def send(self, alert: Alert) -> None:
        _post_json(self.url, {
            "msgtype": "text",
            "text": {"content": f"[{alert.severity}] {alert.kind}: {alert.message}"},
        })


class EmailNotifier:
    channel = "email"

    def __init__(self, host: str, port: int, from_addr: str, to_addrs: str, username: str, password: str, use_tls: bool):
        self.host = host
        self.port = port
        self.from_addr = from_addr
        self.to_addrs = [item.strip() for item in to_addrs.split(",") if item.strip()]
        self.username = username
        self.password = password
        self.use_tls = use_tls

    def send(self, alert: Alert) -> None:
        message = MIMEText(f"[{alert.severity}] {alert.kind}: {alert.message}\nresource: {alert.resource_type}/{alert.resource_id}", "plain", "utf-8")
        message["Subject"] = f"Knowledge Hub alert: {alert.kind}"
        message["From"] = self.from_addr
        message["To"] = ", ".join(self.to_addrs)
        server = smtplib.SMTP(self.host, self.port, timeout=10)
        try:
            if self.use_tls:
                server.starttls()
            if self.username:
                server.login(self.username, self.password)
            server.sendmail(self.from_addr, self.to_addrs, message.as_string())
        finally:
            server.quit()


def build_notifiers() -> list[Notifier]:
    notifiers: list[Notifier] = []
    enabled = settings.enabled_alert_channels
    if "webhook" in enabled and settings.alert_webhook_url:
        notifiers.append(WebhookNotifier(settings.alert_webhook_url))
    if "wecom" in enabled and settings.alert_wecom_webhook:
        notifiers.append(WeComNotifier(settings.alert_wecom_webhook))
    if "dingtalk" in enabled and settings.alert_dingtalk_webhook:
        notifiers.append(DingTalkNotifier(settings.alert_dingtalk_webhook))
    if "email" in enabled and settings.alert_email_smtp_host and settings.alert_email_from and settings.alert_email_to:
        notifiers.append(EmailNotifier(
            settings.alert_email_smtp_host,
            settings.alert_email_smtp_port,
            settings.alert_email_from,
            settings.alert_email_to,
            settings.alert_email_username,
            settings.alert_email_password,
            settings.alert_email_tls,
        ))
    return notifiers


def _notifier_by_channel(channel: str) -> Notifier | None:
    return next((item for item in build_notifiers() if item.channel == channel), None)


def ensure_deliveries(db: Session, alert: Alert) -> list[AlertDelivery]:
    """Reconcile one delivery row per enabled channel, with a re-alert suppression window.

    Repeated alerts share one fingerprint and therefore one alert row. When the
    same alert re-fires within the suppression window after a successful send we
    keep the ``sent`` record and only write an audit entry; after the window the
    row is reset to ``pending`` so the operator gets a fresh notification.
    """
    touched: list[AlertDelivery] = []
    if not alert.id:
        db.flush()
    now = datetime.now(timezone.utc)
    window = timedelta(seconds=settings.alert_suppress_window_seconds)
    for channel in settings.enabled_alert_channels:
        existing = db.scalars(
            select(AlertDelivery).where(AlertDelivery.alert_id == alert.id, AlertDelivery.channel == channel)
        ).first()
        if existing is None:
            delivery = AlertDelivery(alert_id=alert.id, channel=channel, status="pending")
            db.add(delivery)
            touched.append(delivery)
            continue
        if existing.status in {"failed", "dead_letter", "suppressed"}:
            existing.status = "pending"
            existing.last_error = None
            existing.attempts = 0
            existing.updated_at = now
            touched.append(existing)
        elif existing.status == "sent":
            sent_at = existing.sent_at
            if sent_at is not None and sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            if sent_at is not None and (now - sent_at) < window:
                db.add(AuditLog(actor="alerting", action="alert_delivery.suppressed", resource_type="alert_delivery", resource_id=str(existing.id), detail=json.dumps({"channel": channel}, ensure_ascii=False)))
            else:
                existing.status = "pending"
                existing.attempts = 0
                existing.updated_at = now
                touched.append(existing)
    return touched


def _attempt_delivery(db: Session, delivery: AlertDelivery, notifier: Notifier) -> None:
    alert = db.get(Alert, delivery.alert_id)
    if alert is None or alert.status == "resolved":
        # Never deliver a resolved/missing alert; park it so it stops retrying.
        delivery.status = "suppressed"
        delivery.last_error = "alert resolved before delivery" if alert is not None else "alert missing"
        return
    try:
        notifier.send(alert)
    except Exception as exc:
        delivery.attempts += 1
        delivery.last_error = str(exc)
        delivery.status = "failed" if delivery.attempts < settings.alert_retry_max else "dead_letter"
        db.add(AuditLog(actor="alerting", action="alert_delivery.failed", resource_type="alert_delivery", resource_id=str(delivery.id), detail=json.dumps({"channel": delivery.channel, "error": str(exc)}, ensure_ascii=False)))
    else:
        delivery.attempts += 1
        delivery.last_error = None
        delivery.status = "sent"
        delivery.sent_at = datetime.now(timezone.utc)
        db.add(AuditLog(actor="alerting", action="alert_delivery.sent", resource_type="alert_delivery", resource_id=str(delivery.id), detail=json.dumps({"channel": delivery.channel}, ensure_ascii=False)))


def process_alert_deliveries(db: Session, limit: int = 50) -> dict[str, int]:
    """Retry pending/failed deliveries with backoff; move exhausted ones to dead_letter."""
    notifiers = {item.channel: item for item in build_notifiers()}
    if not notifiers:
        return {"processed": 0, "sent": 0, "failed": 0, "dead_letter": 0, "suppressed": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.alert_retry_backoff_seconds)
    rows = db.scalars(
        select(AlertDelivery)
        .where(
            AlertDelivery.status.in_(["pending", "failed"]),
            AlertDelivery.channel.in_(list(notifiers)),
            or_(
                and_(AlertDelivery.status == "pending", AlertDelivery.attempts == 0),
                AlertDelivery.updated_at < cutoff,
            ),
        )
        .order_by(AlertDelivery.created_at, AlertDelivery.id)
        .limit(limit)
    ).all()
    counts = {"processed": 0, "sent": 0, "failed": 0, "dead_letter": 0, "suppressed": 0}
    for delivery in rows:
        notifier = notifiers.get(delivery.channel)
        if notifier is None:
            continue
        if delivery.status == "pending" and delivery.attempts >= settings.alert_retry_max:
            delivery.status = "dead_letter"
            counts["dead_letter"] += 1
            continue
        _attempt_delivery(db, delivery, notifier)
        counts["processed"] += 1
        counts[delivery.status] = counts.get(delivery.status, 0) + 1
    db.commit()
    return counts


def alert_delivery_summary(db: Session) -> dict[str, Any]:
    from sqlalchemy import func

    rows = db.execute(
        select(AlertDelivery.channel, AlertDelivery.status, func.count(AlertDelivery.id))
        .group_by(AlertDelivery.channel, AlertDelivery.status)
    ).all()
    summary: dict[str, Any] = {"channels": settings.enabled_alert_channels, "counts": {}}
    for channel, status, count in rows:
        summary["counts"].setdefault(channel, {})[status] = count
    return summary

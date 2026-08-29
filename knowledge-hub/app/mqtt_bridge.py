import json
import logging
import re
import ssl
import time
from dataclasses import dataclass

import httpx
import paho.mqtt.client as mqtt
from pydantic import ValidationError

from .config import settings
from .schemas import NodeStatusEvent


LOGGER = logging.getLogger("knowledge.mqtt_bridge")
STATUS_TOPIC = re.compile(r"^knowledge/nodes/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/status$")


@dataclass(frozen=True)
class ValidatedStatus:
    node_id: str
    payload: NodeStatusEvent


def validate_status_message(topic: str, raw_payload: bytes) -> ValidatedStatus:
    match = STATUS_TOPIC.fullmatch(topic)
    if not match:
        raise ValueError("unexpected MQTT topic")
    try:
        body = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid MQTT JSON payload") from exc
    if body.get("node_id") != match.group(1):
        raise ValueError("MQTT topic and payload node IDs do not match")
    if body.get("status") == "offline":
        body.setdefault("hostname", body["node_id"])
        body.setdefault("agent_version", "unknown")
        body.setdefault("cpu_percent", 0)
        body.setdefault("disk_free_bytes", 0)
        body.setdefault("is_replica", False)
    try:
        event = NodeStatusEvent.model_validate(body)
    except ValidationError as exc:
        raise ValueError("invalid MQTT node status event") from exc
    return ValidatedStatus(node_id=match.group(1), payload=event)


def forward_status(client: httpx.Client, message: ValidatedStatus) -> None:
    response = client.post(
        f"{settings.hub_internal_url.rstrip('/')}/internal/v1/mqtt/node-status",
        headers={"X-Bridge-Key": settings.mqtt_bridge_api_key},
        json=message.payload.model_dump(mode="json"),
    )
    response.raise_for_status()


def build_client(http_client: httpx.Client) -> mqtt.Client:
    if not settings.mqtt_tls_enabled:
        raise RuntimeError("MQTT bridge requires MQTT_TLS_ENABLED=true")
    required = {
        "MQTT_USERNAME": settings.mqtt_username,
        "MQTT_PASSWORD": settings.mqtt_password,
        "MQTT_CA_FILE": settings.mqtt_ca_file,
        "MQTT_BRIDGE_API_KEY": settings.mqtt_bridge_api_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"missing MQTT bridge configuration: {', '.join(missing)}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="knowledge-hub-status-bridge")
    client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    client.tls_set(
        ca_certs=settings.mqtt_ca_file,
        certfile=settings.mqtt_client_cert_file or None,
        keyfile=settings.mqtt_client_key_file or None,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    def on_connect(active_client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        active_client.subscribe("knowledge/nodes/+/status", qos=1)
        LOGGER.info("subscribed to authenticated node status events")

    def on_message(_active_client, _userdata, message):
        try:
            validated = validate_status_message(message.topic, message.payload)
            forward_status(http_client, validated)
        except (ValueError, httpx.HTTPError):
            LOGGER.exception("rejected MQTT node status event topic=%s", message.topic)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with httpx.Client(timeout=10) as http_client:
        client = build_client(http_client)
        client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        try:
            client.loop_forever(retry_first_connection=True)
        finally:
            client.disconnect()
            time.sleep(0.1)


if __name__ == "__main__":
    main()

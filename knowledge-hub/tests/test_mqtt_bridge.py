import json

import pytest

from app.mqtt_bridge import validate_status_message


def test_validates_online_status_topic_and_payload():
    message = validate_status_message(
        "knowledge/nodes/node-1/status",
        json.dumps({
            "node_id": "node-1",
            "status": "online",
            "hostname": "pc-1",
            "agent_version": "0.2.0",
            "cpu_percent": 2.5,
            "disk_free_bytes": 1024,
            "is_replica": True,
        }).encode(),
    )
    assert message.node_id == "node-1"
    assert message.payload.is_replica is True


def test_accepts_minimal_offline_will():
    message = validate_status_message(
        "knowledge/nodes/node-1/status",
        b'{"node_id":"node-1","status":"offline","reason":"mqtt_will"}',
    )
    assert message.payload.status == "offline"
    assert message.payload.hostname == "node-1"


@pytest.mark.parametrize("topic", [
    "knowledge/nodes/../status",
    "knowledge/nodes/node-1/commands",
    "other/nodes/node-1/status",
])
def test_rejects_unexpected_topics(topic):
    with pytest.raises(ValueError):
        validate_status_message(topic, b'{"node_id":"node-1","status":"offline"}')


def test_rejects_topic_payload_node_mismatch():
    with pytest.raises(ValueError, match="do not match"):
        validate_status_message(
            "knowledge/nodes/node-1/status",
            b'{"node_id":"node-2","status":"offline"}',
        )

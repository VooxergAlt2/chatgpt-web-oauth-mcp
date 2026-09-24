#!/usr/bin/env python3
"""External Ops MCP health -> MQTT publisher for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import signal
import ssl
import threading
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

LOG = logging.getLogger("ops_mcp_ha_monitor")
EVENT_TYPES = [
    "server_offline",
    "server_online",
    "session_stalled",
    "session_orchestration_quiet",
    "session_recovered",
    "delegate_stalled",
    "delegate_recovered",
    "health_state_changed",
]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass(frozen=True)
class Config:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_tls: bool
    mqtt_keepalive: int
    base_topic: str
    discovery_prefix: str
    discovery_object_id: str
    health_url: str
    health_token: str
    poll_interval: float
    http_timeout: float
    detail_session_limit: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            mqtt_host=os.environ.get("MQTT_HOST", "").strip(),
            mqtt_port=int(_float_env("MQTT_PORT", 1883)),
            mqtt_username=os.environ.get("MQTT_USERNAME", "").strip() or None,
            mqtt_password=os.environ.get("MQTT_PASSWORD", "").strip() or None,
            mqtt_tls=_bool_env("MQTT_TLS", False),
            mqtt_keepalive=int(_float_env("MQTT_KEEPALIVE", 60)),
            base_topic=(
                os.environ.get("OPS_MCP_MQTT_BASE_TOPIC", "gip-core/ops-mcp")
                .strip()
                .rstrip("/")
                or "gip-core/ops-mcp"
            ),
            discovery_prefix=(
                os.environ.get("OPS_MCP_HA_DISCOVERY_PREFIX", "homeassistant").strip().rstrip("/")
                or "homeassistant"
            ),
            discovery_object_id=(
                os.environ.get("OPS_MCP_HA_DISCOVERY_OBJECT_ID", "gip_core_ops_mcp").strip()
                or "gip_core_ops_mcp"
            ),
            health_url=(
                os.environ.get(
                    "OPS_MCP_HEALTH_URL",
                    "http://127.0.0.1:8770/internal/health",
                ).strip()
            ),
            health_token=os.environ.get("CHATGPT_MCP_HEALTH_TOKEN", "").strip(),
            poll_interval=max(2.0, _float_env("OPS_MCP_HA_POLL_INTERVAL", 10.0)),
            http_timeout=max(0.5, _float_env("OPS_MCP_HA_HTTP_TIMEOUT", 3.0)),
            detail_session_limit=max(
                1,
                int(_float_env("OPS_MCP_HA_DETAIL_SESSION_LIMIT", 12)),
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )


def fetch_health(config: Config) -> dict[str, Any]:
    req = urlrequest.Request(
        config.health_url,
        headers={
            "Accept": "application/json",
            "X-Ops-Health-Token": config.health_token,
        },
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=config.http_timeout) as response:
            raw = response.read()
    except (OSError, urlerror.URLError, urlerror.HTTPError) as exc:
        raise RuntimeError(f"health fetch failed: {type(exc).__name__}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"health response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("health response is not a JSON object")
    return payload


def _component(
    *,
    platform: str,
    unique_id: str,
    name: str,
    value_template: str | None = None,
    icon: str | None = None,
    json_attributes_topic: str | None = None,
    state_topic: str | None = None,
    event_types: list[str] | None = None,
    state_class: str | None = None,
    entity_category: str | None = None,
    unit_of_measurement: str | None = None,
    suggested_display_precision: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "p": platform,
        "unique_id": unique_id,
        "name": name,
    }
    if value_template:
        payload["value_template"] = value_template
    if icon:
        payload["icon"] = icon
    if json_attributes_topic:
        payload["json_attributes_topic"] = json_attributes_topic
    if state_topic:
        payload["state_topic"] = state_topic
    if event_types:
        payload["event_types"] = event_types
    if state_class:
        payload["state_class"] = state_class
    if entity_category:
        payload["entity_category"] = entity_category
    if unit_of_measurement:
        payload["unit_of_measurement"] = unit_of_measurement
    if suggested_display_precision is not None:
        payload["suggested_display_precision"] = suggested_display_precision
    return payload


def build_discovery_payload(config: Config) -> dict[str, Any]:
    base = config.base_topic
    components = {
        "state": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_state",
            name="State",
            value_template="{{ value_json.state }}",
            icon="mdi:server",
            entity_category="diagnostic",
        ),
        "problem": _component(
            platform="binary_sensor",
            unique_id="gip_core_ops_mcp_problem",
            name="Problem",
            value_template=(
                "{{ 'ON' if value_json.state in ['degraded','stalled','offline'] else 'OFF' }}"
            ),
            icon="mdi:alert-circle-outline",
        ),
        "sessions": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_sessions",
            name="Active sessions",
            value_template="{{ value_json.summary.sessions }}",
            json_attributes_topic=f"{base}/detail",
            icon="mdi:account-multiple-outline",
            state_class="measurement",
            unit_of_measurement="sessions",
            suggested_display_precision=0,
        ),
        "sessions_retained": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_sessions_retained",
            name="Retained sessions",
            value_template="{{ value_json.summary.sessions_retained }}",
            icon="mdi:archive-clock-outline",
            state_class="measurement",
            unit_of_measurement="sessions",
            suggested_display_precision=0,
            entity_category="diagnostic",
        ),
        "sessions_quiet": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_sessions_quiet",
            name="Orchestration quiet sessions",
            value_template="{{ value_json.summary.sessions_orchestration_quiet }}",
            icon="mdi:account-question-outline",
        ),
        "sessions_stalled": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_sessions_stalled",
            name="Stalled sessions",
            value_template="{{ value_json.summary.sessions_stalled }}",
            icon="mdi:account-alert-outline",
        ),
        "delegates_active": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_delegates_active",
            name="Active delegates",
            value_template="{{ value_json.summary.delegates_active }}",
            icon="mdi:robot-outline",
        ),
        "delegates_stalled": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_delegates_stalled",
            name="Stalled delegates",
            value_template="{{ value_json.summary.delegates_stalled }}",
            icon="mdi:robot-confused-outline",
        ),
        "jobs_running": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_jobs_running",
            name="Running jobs",
            value_template="{{ value_json.summary.jobs_running }}",
            icon="mdi:cog-clockwise",
        ),
        "jobs_stalled": _component(
            platform="sensor",
            unique_id="gip_core_ops_mcp_jobs_stalled",
            name="Stalled jobs",
            value_template="{{ value_json.summary.jobs_stalled }}",
            icon="mdi:cog-pause-outline",
        ),
        "activity_event": _component(
            platform="event",
            unique_id="gip_core_ops_mcp_activity_event",
            name="Activity event",
            state_topic=f"{base}/event",
            event_types=EVENT_TYPES,
            icon="mdi:timeline-alert-outline",
        ),
    }
    return {
        "dev": {
            "ids": ["gip_core_ops_mcp"],
            "name": "GIP Core Ops MCP",
            "mf": "DeM",
            "mdl": "Ops MCP Health",
        },
        "o": {
            "name": "chatgpt-web-oauth-mcp-ha-monitor",
            "sw": "1.0",
        },
        "availability_topic": f"{base}/monitor/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "state_topic": f"{base}/state",
        "cmps": components,
    }


def build_discovery_component_removal_payload(
    config: Config,
    *,
    component_id: str,
    platform: str,
) -> dict[str, Any]:
    return {
        "dev": {
            "ids": ["gip_core_ops_mcp"],
            "name": "GIP Core Ops MCP",
            "mf": "DeM",
            "mdl": "Ops MCP Health",
        },
        "o": {
            "name": "chatgpt-web-oauth-mcp-ha-monitor",
            "sw": "1.0",
        },
        "cmps": {
            component_id: {
                "p": platform,
            }
        },
    }


def discovery_topic(config: Config) -> str:
    return (
        f"{config.discovery_prefix}/device/"
        f"{config.discovery_object_id}/config"
    )


def state_payload(health: dict[str, Any]) -> dict[str, Any]:
    summary = health.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    defaults = {
        "sessions": 0,
        "sessions_active": 0,
        "sessions_retained": 0,
        "sessions_inflight": 0,
        "sessions_idle": 0,
        "sessions_orchestration_quiet": 0,
        "sessions_stalled": 0,
        "delegates_active": 0,
        "delegates_quiet": 0,
        "delegates_stalled": 0,
        "delegates_queued": 0,
        "jobs_running": 0,
        "jobs_active": 0,
        "jobs_quiet": 0,
        "jobs_stalled": 0,
    }
    defaults.update({key: value for key, value in summary.items() if key in defaults})
    return {
        "state": str(health.get("state") or "degraded"),
        "timestamp": health.get("timestamp")
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": defaults,
        "server_pid": health.get("pid"),
        "server_uptime_seconds": health.get("uptime_seconds"),
        "data_stale": False,
    }


def offline_payload(
    *,
    last_state: dict[str, Any] | None,
    error: str,
) -> dict[str, Any]:
    payload = state_payload(last_state or {})
    payload["state"] = "offline"
    payload["timestamp"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["data_stale"] = True
    payload["error"] = error
    return payload


def detail_payload(health: dict[str, Any], *, session_limit: int) -> dict[str, Any]:
    sessions = health.get("sessions")
    delegates = health.get("delegates")
    jobs = health.get("jobs")
    safe_sessions: list[dict[str, Any]] = []
    if isinstance(sessions, list):
        active_sessions = [
            item
            for item in sessions
            if isinstance(item, dict) and bool(item.get("is_active"))
        ]
        for item in active_sessions[:session_limit]:
            safe_sessions.append(
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "state",
                        "project",
                        "current_tool",
                        "request_age_seconds",
                        "last_tool",
                        "required_action",
                        "required_action_age_seconds",
                        "last_execution_state",
                        "last_seen_seconds_ago",
                        "is_active",
                    )
                }
            )
    return {
        "sessions": safe_sessions,
        "sessions_truncated": bool(health.get("sessions_truncated"))
        or (
            isinstance(sessions, list)
            and sum(
                isinstance(item, dict) and bool(item.get("is_active"))
                for item in sessions
            )
            > session_limit
        ),
        "delegates": delegates if isinstance(delegates, list) else [],
        "jobs": jobs if isinstance(jobs, list) else [],
        "observation_errors": (
            health.get("observation_errors")
            if isinstance(health.get("observation_errors"), dict)
            else {}
        ),
        "thresholds": (
            health.get("thresholds")
            if isinstance(health.get("thresholds"), dict)
            else {}
        ),
    }


class OpsMcpHaMonitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._stop = threading.Event()
        self._client = self._build_client()
        self._previous_state: str | None = None
        self._previous_sessions: dict[str, str] = {}
        self._previous_delegates: dict[str, str] = {}
        self._last_health: dict[str, Any] | None = None

    def _build_client(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("paho-mqtt is required for the HA monitor") from exc

        kwargs = {"client_id": f"ops-mcp-ha-monitor-{os.uname().nodename}"}
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
        except AttributeError:
            client = mqtt.Client(**kwargs)
        if self.config.mqtt_username:
            client.username_pw_set(
                self.config.mqtt_username,
                self.config.mqtt_password,
            )
        if self.config.mqtt_tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.will_set(
            f"{self.config.base_topic}/monitor/status",
            payload="offline",
            qos=0,
            retain=True,
        )
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        failed = getattr(reason_code, "is_failure", False)
        if isinstance(reason_code, int):
            failed = reason_code != 0
        if failed:
            LOG.warning("MQTT connect failed: %s", reason_code)
            return
        LOG.info("MQTT connected to %s:%s", self.config.mqtt_host, self.config.mqtt_port)
        client.publish(
            f"{self.config.base_topic}/monitor/status",
            "online",
            qos=0,
            retain=True,
        )
        client.subscribe(f"{self.config.discovery_prefix}/status", qos=0)
        self._publish_discovery()

    def _on_disconnect(self, client, userdata, *args) -> None:
        LOG.warning("MQTT disconnected; reconnect is automatic")

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8").strip().lower()
        except Exception:
            return
        if (
            message.topic == f"{self.config.discovery_prefix}/status"
            and payload == "online"
        ):
            self._publish_discovery()

    def _publish_discovery(self) -> None:
        topic = discovery_topic(self.config)
        # Device discovery keeps omitted components unless they are explicitly
        # removed first. This one-shot-compatible removal is safe to repeat.
        self._client.publish(
            topic,
            json.dumps(
                build_discovery_component_removal_payload(
                    self.config,
                    component_id="sessions_active",
                    platform="sensor",
                ),
                ensure_ascii=False,
            ),
            qos=0,
            retain=True,
        )
        self._client.publish(
            topic,
            json.dumps(build_discovery_payload(self.config), ensure_ascii=False),
            qos=0,
            retain=True,
        )

    def _publish_event(self, event_type: str, **attributes: Any) -> None:
        payload = {
            "event_type": event_type,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            **attributes,
        }
        self._client.publish(
            f"{self.config.base_topic}/event",
            json.dumps(payload, ensure_ascii=False),
            qos=0,
            retain=False,
        )

    def _publish_transitions(self, health: dict[str, Any]) -> None:
        state = str(health.get("state") or "degraded")
        if self._previous_state is not None and state != self._previous_state:
            self._publish_event(
                "health_state_changed",
                previous=self._previous_state,
                current=state,
            )

        sessions = health.get("sessions")
        current_sessions: dict[str, str] = {}
        if isinstance(sessions, list):
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                session_id = str(item.get("id") or "")
                session_state = str(item.get("state") or "")
                if not session_id:
                    continue
                current_sessions[session_id] = session_state
                previous = self._previous_sessions.get(session_id)
                if session_state == "stalled_request" and previous != session_state:
                    self._publish_event(
                        "session_stalled",
                        session=session_id,
                        project=item.get("project"),
                        tool=item.get("current_tool"),
                        age_s=item.get("request_age_seconds"),
                    )
                elif (
                    session_state == "orchestration_quiet"
                    and previous != session_state
                ):
                    self._publish_event(
                        "session_orchestration_quiet",
                        session=session_id,
                        project=item.get("project"),
                        tool=item.get("last_tool"),
                        required_action=item.get("required_action"),
                        age_s=item.get("required_action_age_seconds"),
                    )
                elif (
                    previous in {"stalled_request", "orchestration_quiet"}
                    and session_state not in {"stalled_request", "orchestration_quiet"}
                ):
                    self._publish_event(
                        "session_recovered",
                        session=session_id,
                        project=item.get("project"),
                        state=session_state,
                    )

        for session_id, previous in self._previous_sessions.items():
            if (
                session_id not in current_sessions
                and previous in {"stalled_request", "orchestration_quiet"}
            ):
                self._publish_event(
                    "session_recovered",
                    session=session_id,
                    state="closed",
                )

        delegates = health.get("delegates")
        current_delegates: dict[str, str] = {}
        if isinstance(delegates, list):
            for item in delegates:
                if not isinstance(item, dict):
                    continue
                delegate_id = str(item.get("delegate_id") or "")
                delegate_state = str(item.get("activity_state") or "")
                if not delegate_id:
                    continue
                current_delegates[delegate_id] = delegate_state
                previous = self._previous_delegates.get(delegate_id)
                if delegate_state == "suspected_stalled" and previous != delegate_state:
                    self._publish_event(
                        "delegate_stalled",
                        delegate=delegate_id,
                        project=item.get("project"),
                        harness=item.get("harness"),
                        age_s=item.get("last_output_seconds_ago"),
                    )
                elif (
                    previous == "suspected_stalled"
                    and delegate_state != "suspected_stalled"
                ):
                    self._publish_event(
                        "delegate_recovered",
                        delegate=delegate_id,
                        project=item.get("project"),
                        harness=item.get("harness"),
                        state=delegate_state,
                    )

        for delegate_id, previous in self._previous_delegates.items():
            if (
                delegate_id not in current_delegates
                and previous == "suspected_stalled"
            ):
                self._publish_event(
                    "delegate_recovered",
                    delegate=delegate_id,
                    state="completed_or_closed",
                )

        self._previous_state = state
        self._previous_sessions = current_sessions
        self._previous_delegates = current_delegates

    def _publish_health(self, health: dict[str, Any]) -> None:
        self._publish_transitions(health)
        state = state_payload(health)
        details = detail_payload(
            health,
            session_limit=self.config.detail_session_limit,
        )
        self._client.publish(
            f"{self.config.base_topic}/state",
            json.dumps(state, ensure_ascii=False),
            qos=0,
            retain=True,
        )
        self._client.publish(
            f"{self.config.base_topic}/detail",
            json.dumps(details, ensure_ascii=False),
            qos=0,
            retain=True,
        )
        self._last_health = health

    def _publish_offline(self, error: str) -> None:
        if self._previous_state != "offline":
            self._publish_event("server_offline", error=error)
        payload = offline_payload(last_state=self._last_health, error=error)
        self._client.publish(
            f"{self.config.base_topic}/state",
            json.dumps(payload, ensure_ascii=False),
            qos=0,
            retain=True,
        )
        self._client.publish(
            f"{self.config.base_topic}/detail",
            json.dumps({"error": error, "data_stale": True}, ensure_ascii=False),
            qos=0,
            retain=True,
        )
        self._previous_state = "offline"

    def run(self) -> None:
        if not self.config.mqtt_host:
            raise RuntimeError("MQTT_HOST is required")
        if not self.config.health_token:
            raise RuntimeError("CHATGPT_MCP_HEALTH_TOKEN is required")
        self._client.connect_async(
            self.config.mqtt_host,
            self.config.mqtt_port,
            self.config.mqtt_keepalive,
        )
        self._client.loop_start()
        try:
            while not self._stop.is_set():
                try:
                    health = fetch_health(self.config)
                except Exception as exc:
                    LOG.warning("Ops MCP health fetch failed: %s", exc)
                    self._publish_offline(str(exc))
                else:
                    if self._previous_state == "offline":
                        self._publish_event("server_online")
                    self._publish_health(health)
                self._stop.wait(self.config.poll_interval)
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        try:
            self._client.publish(
                f"{self.config.base_topic}/monitor/status",
                "offline",
                qos=0,
                retain=True,
            )
            time.sleep(0.2)
        finally:
            self._client.loop_stop()
            self._client.disconnect()


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = OpsMcpHaMonitor(config)

    def _stop(_signum, _frame) -> None:
        service.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    service.run()


if __name__ == "__main__":
    main()

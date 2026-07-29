#!/usr/bin/env python3
"""MQTT fan-out + Home Assistant discovery for condfy-bridge.

Publishes to the Mosquitto add-on on bnu-homeassistant (10.1.1.124). Everything
Home Assistant needs is created through MQTT discovery, so there is no package
YAML to deploy.

Retain policy — the one thing that must not be got wrong: `.../event` is NOT
retained, because a retained event replays on every HA reconnect and would
re-fire automations. The retained copy of the same data lives in `.../last_event`,
which is a *state* and is meant to be replayed.
"""
import json
import logging

import paho.mqtt.client as mqtt

from condfy import slug

log = logging.getLogger(__name__)

BASE = "condfy/ara"
T_STATUS = f"{BASE}/status"
T_EVENT = f"{BASE}/event"
T_LAST_EVENT = f"{BASE}/last_event"
T_HEALTH = f"{BASE}/bridge/state"
T_NOTICE = f"{BASE}/notice"

DEVICE = {
    "identifiers": ["condfy_bridge_ara"],
    "name": "Condfy — Céu Azul (ARA)",
    "manufacturer": "Condfy",
    "model": "condfy-bridge",
}
AVAILABILITY = {
    "availability_topic": T_STATUS,
    "payload_available": "online",
    "payload_not_available": "offline",
}


def person_topic(person_key):
    return f"{BASE}/person/{slug(person_key)}/state"


def gate_topic(gate_key):
    return f"{BASE}/gate/{slug(gate_key)}/state"


class MqttBridge:
    """Thin paho wrapper: connect, publish, and re-assert state on every connect.

    `on_ready` is invoked after each successful (re)connect, once discovery and
    the online marker are out — the app uses it to republish current snapshots
    from SQLite so a broker restart that lost its retained set self-heals.
    """

    def __init__(self, host, port, user, password, discovery_prefix="homeassistant",
                 watch_names=(), version="1.0.0", on_ready=None):
        self.host, self.port = host, int(port)
        self.prefix = discovery_prefix.rstrip("/")
        self.watch_names = list(watch_names)
        self.version = version
        self.on_ready = on_ready
        self.connected = False

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="condfy-bridge", protocol=mqtt.MQTTv311)
        if user:
            self.client.username_pw_set(user, password)
        self.client.will_set(T_STATUS, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # -- lifecycle -------------------------------------------------------- #
    def connect(self):
        """Non-blocking connect: a broker that is down must not stop the poller.

        connect_async + loop_start lets paho retry in its own thread, so the
        service keeps polling and still sends WhatsApp alerts while MQTT is out.
        """
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        try:
            self.client.connect_async(self.host, self.port, keepalive=60)
            self.client.loop_start()
        except Exception as exc:
            log.error("MQTT setup failed (%s) — continuing without it", exc)

    def close(self):
        try:
            self.publish(T_STATUS, "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        log.info("MQTT connected to %s:%s", self.host, self.port)
        self._publish_discovery()
        self.publish(T_STATUS, "online", retain=True)
        if self.on_ready:
            try:
                self.on_ready()
            except Exception:
                log.exception("on_ready failed")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected (%s) — paho will retry", reason_code)

    # -- publishing ------------------------------------------------------- #
    def publish(self, topic, payload, retain=False, qos=1):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def publish_event(self, event):
        """A new access event: fire-and-forget (NOT retained) + retained snapshot."""
        self.publish(T_EVENT, event, retain=False)
        self.publish(T_LAST_EVENT, event, retain=True)

    def publish_person(self, person, payload):
        self.publish(person_topic(person), payload, retain=True)

    def publish_gate(self, gate, payload):
        self.publish(gate_topic(gate), payload, retain=True)

    def publish_health(self, payload):
        self.publish(T_HEALTH, payload, retain=True)

    def publish_notice(self, payload):
        self.publish(T_NOTICE, payload, retain=True)

    # -- discovery -------------------------------------------------------- #
    def _discovery(self, domain, object_id, config):
        config.update({"device": DEVICE, "unique_id": f"condfy_ara_{object_id}"})
        topic = f"{self.prefix}/{domain}/condfy_ara/{object_id}/config"
        self.publish(topic, config, retain=True)

    def _publish_discovery(self):
        self._discovery("sensor", "ultimo_acesso", {
            "name": "Último acesso",
            "state_topic": T_LAST_EVENT,
            "value_template": "{{ value_json.person | default('desconhecido', true) }}",
            "json_attributes_topic": T_LAST_EVENT,
            "icon": "mdi:door-open",
            **AVAILABILITY,
        })

        for name in self.watch_names:
            key = slug(name)
            self._discovery("sensor", f"{key}_last_seen", {
                "name": f"{name} visto em",
                "state_topic": person_topic(name),
                "value_template": "{{ value_json.last_seen }}",
                "json_attributes_topic": person_topic(name),
                "device_class": "timestamp",
                "icon": "mdi:account-clock",
                **AVAILABILITY,
            })

        # No availability_topic here on purpose: this entity IS the availability
        # signal, and it must read off rather than unavailable when the bridge dies.
        self._discovery("binary_sensor", "online", {
            "name": "Bridge online",
            "state_topic": T_STATUS,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
        })

        self._discovery("sensor", "ultima_coleta", {
            "name": "Última coleta",
            "state_topic": T_HEALTH,
            "value_template": "{{ value_json.last_success }}",
            "json_attributes_topic": T_HEALTH,
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            **AVAILABILITY,
        })

        # Renders 'ON'/'OFF' strings deliberately — {{ value_json.login_failed }}
        # would render Python's True/False and match neither default payload.
        self._discovery("binary_sensor", "login_problema", {
            "name": "Problema de login",
            "state_topic": T_HEALTH,
            "value_template": "{{ 'ON' if value_json.login_failed else 'OFF' }}",
            "device_class": "problem",
            "entity_category": "diagnostic",
            **AVAILABILITY,
        })
        log.info("published HA discovery for %d entities", 4 + len(self.watch_names))

"""Output til Home Assistant: MQTT discovery (permanente sensorer) og
notifikationer via Supervisor-proxyen til HA Core."""

from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("skoleintra.out")

SUPERVISOR = "http://supervisor"
DEVICE = {
    "identifiers": ["skoleintra_addon"],
    "name": "SkoleIntra",
    "manufacturer": "ForældreIntra",
    "model": "Ugeplan-scraper",
}
# Enhedsnavn "SkoleIntra" + entitetsnavn "Ugeplan" -> sensor.skoleintra_ugeplan
SENSORS = {
    "ugeplan": {"name": "Ugeplan", "icon": "mdi:school", "attrs": True},
    "status": {"name": "Status", "icon": "mdi:information-outline", "attrs": True,
               "entity_category": "diagnostic"},
    "sidst_hentet": {"name": "Sidst hentet", "device_class": "timestamp",
                     "entity_category": "diagnostic"},
}


def _token() -> str:
    t = os.environ.get("SUPERVISOR_TOKEN")
    if not t:
        raise RuntimeError("SUPERVISOR_TOKEN mangler - kører add-on'en under Supervisor?")
    return t


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def mqtt_broker() -> dict:
    r = requests.get(f"{SUPERVISOR}/services/mqtt", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def _discovery_messages() -> list[dict]:
    msgs = []
    for key, s in SENSORS.items():
        cfg = {
            "name": s["name"],
            "unique_id": f"skoleintra_{key}",
            "state_topic": f"skoleintra/{key}/state",
            "device": DEVICE,
        }
        if s.get("attrs"):
            cfg["json_attributes_topic"] = f"skoleintra/{key}/attributes"
        for opt in ("icon", "device_class", "entity_category"):
            if opt in s:
                cfg[opt] = s[opt]
        msgs.append({"topic": f"homeassistant/sensor/skoleintra/{key}/config",
                     "payload": json.dumps(cfg, ensure_ascii=False), "retain": True})
    return msgs


def publish(values: dict[str, tuple[str, dict | None]]) -> None:
    """values: {"ugeplan": (state, attributes), ...}. Alt sendes retained."""
    import paho.mqtt.publish as mqtt_publish

    b = mqtt_broker()
    msgs = _discovery_messages()
    for key, (state, attrs) in values.items():
        msgs.append({"topic": f"skoleintra/{key}/state", "payload": str(state)[:255], "retain": True})
        if attrs is not None:
            msgs.append({"topic": f"skoleintra/{key}/attributes",
                         "payload": json.dumps(attrs, ensure_ascii=False), "retain": True})
    auth = {"username": b["username"], "password": b["password"]} if b.get("username") else None
    mqtt_publish.multiple(msgs, hostname=b["host"], port=int(b["port"]), auth=auth,
                          client_id="skoleintra-addon")
    log.info("MQTT: publicerede %s", ", ".join(values))


def notify(service: str, title: str, message: str) -> None:
    domain, _, name = service.partition(".")
    r = requests.post(
        f"{SUPERVISOR}/core/api/services/{domain}/{name}",
        headers=_headers(),
        json={"title": title, "message": message[:4000]},
        timeout=30,
    )
    r.raise_for_status()
    log.info("Notifikation sendt via %s", service)

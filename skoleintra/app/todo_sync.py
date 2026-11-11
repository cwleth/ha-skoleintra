"""Synkroniserer lektiebogen til to HA-to-do-lister.

todo.lektier          - én post pr. lektie (fag != Info) med lektiedagen som frist.
todo.lektier_backlog  - lektier der ikke er krydset af, når fristen er passeret.

Barnet krydser selv af. Add-on'en ejer kun poster den selv har oprettet
(sporet via uid i /data/todo_sync.json) og rører ikke manuelt tilføjede poster
ud over oprydning af gamle afkrydsede.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import outputs

log = logging.getLogger("skoleintra.todo")

MAIN = "todo.lektier"
BACKLOG = "todo.lektier_backlog"
KEEP_DONE_DAYS = 7      # afkrydsede i hovedlisten ryddes efter en uge
KEEP_BACKLOG_DONE_DAYS = 14


def _items(entity: str) -> list[dict]:
    resp = outputs.call_service("todo", "get_items", {"entity_id": entity}, return_response=True) or {}
    return (resp.get(entity) or {}).get("items", [])


DESC_VERSION = "2"  # hæv for at genskrive beskrivelser på åbne poster


def _hash(text: str) -> str:
    return hashlib.sha256((DESC_VERSION + text).encode()).hexdigest()[:16]


def _description(lesson: dict) -> str:
    """Fuld tekst uden fyldlinjer - og tom, hvis overskriften allerede siger det hele."""
    from .parser import _DROP

    lines = [ln.strip() for ln in lesson["content"].split("\n")]
    lines = [ln for ln in lines if ln and not _DROP.match(ln)]
    text = "\n".join(lines)
    if len(lines) <= 1 and len(text) <= 90:
        return ""
    return text


def _summary(lesson: dict) -> str:
    return f"{lesson.get('abbr') or lesson['subject']}: {lesson.get('short') or lesson['content'][:70]}"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def sync(diary: dict, today: date, state_file: Path) -> dict:
    state = _load(state_file)
    stats = {"added": 0, "updated": 0, "removed": 0, "moved": 0, "cleaned": 0}
    main_items = {i["uid"]: i for i in _items(MAIN)}

    # 1-2: tilføj nye / opdater ændrede (kun åbne poster i hovedlisten)
    current_keys = set()
    for d in diary.get("days", []):
        if d["date"] < today.isoformat():
            continue
        for l in d.get("lessons", []):
            if l["subject"] == "Info":
                continue
            key = f"{d['date']}|{l['subject']}"
            current_keys.add(key)
            summ, h = _summary(l), _hash(l["content"])
            entry = state.get(key)
            if entry is None:
                known = set(main_items)
                outputs.call_service("todo", "add_item", {
                    "entity_id": MAIN, "item": summ, "due_date": d["date"], "description": _description(l)})
                main_items = {i["uid"]: i for i in _items(MAIN)}
                new = [i for uid, i in main_items.items()
                       if uid not in known and i.get("summary") == summ and i.get("due") == d["date"]]
                if new:
                    state[key] = {"uid": new[0]["uid"], "hash": h, "list": MAIN, "date": d["date"]}
                    stats["added"] += 1
                else:
                    log.warning("Kunne ikke finde uid for ny post %r", summ)
            elif entry.get("list") == MAIN and entry.get("hash") != h:
                item = main_items.get(entry["uid"])
                if item and item.get("status") == "needs_action":
                    outputs.call_service("todo", "update_item", {
                        "entity_id": MAIN, "item": entry["uid"], "rename": summ, "description": _description(l)})
                    stats["updated"] += 1
                entry["hash"] = h

    # 3: fremtidige lektier som læreren har fjernet
    for key, entry in list(state.items()):
        if entry.get("list") == MAIN and entry["date"] >= today.isoformat() and key not in current_keys:
            item = main_items.get(entry["uid"])
            if item and item.get("status") == "needs_action":
                outputs.call_service("todo", "remove_item", {"entity_id": MAIN, "item": [entry["uid"]]})
                stats["removed"] += 1
            state.pop(key)

    # 4: overskredne, ikke afkrydsede -> backlog (alle poster med frist, også manuelle)
    for uid, item in list(main_items.items()):
        due = item.get("due")
        if item.get("status") == "needs_action" and due and due < today.isoformat():
            outputs.call_service("todo", "add_item", {
                "entity_id": BACKLOG, "item": item["summary"], "due_date": due,
                "description": item.get("description") or ""})
            outputs.call_service("todo", "remove_item", {"entity_id": MAIN, "item": [uid]})
            for entry in state.values():
                if entry.get("uid") == uid:
                    entry["list"] = BACKLOG
            stats["moved"] += 1

    # 5: oprydning af gamle afkrydsede
    for entity, keep in ((MAIN, KEEP_DONE_DAYS), (BACKLOG, KEEP_BACKLOG_DONE_DAYS)):
        cutoff = (today - timedelta(days=keep)).isoformat()
        old = [i["uid"] for i in _items(entity)
               if i.get("status") == "completed" and i.get("due") and i["due"] < cutoff]
        if old:
            outputs.call_service("todo", "remove_item", {"entity_id": entity, "item": old})
            stats["cleaned"] += len(old)

    # tilstand ældre end 30 dage glemmes
    cutoff = (today - timedelta(days=30)).isoformat()
    state = {k: v for k, v in state.items() if v.get("date", "") >= cutoff}
    state_file.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    log.info("To-do: %s", ", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats

"""Synkroniserer lektiebogen til HA-to-do-lister - én liste pr. kommende skoledag.

todo.lektier_dag_1..5  rullende: dag 1 = første dag fra i dag med lektier, dag 2 = næste, ...
todo.lektier_backlog   lektier der ikke er krydset af, når dagen er passeret.

Hver kørsel tildeler lektierne til den rigtige dag-liste. Rykker en dag (fx fra
dag 2 til dag 1 næste morgen), flyttes posten - med sin afkrydsning. barnet
krydser selv af. Kun egne poster (sporet via uid) røres.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import outputs, parser

log = logging.getLogger("skoleintra.todo")

SLOTS = [f"todo.lektier_dag_{i}" for i in range(1, 6)]
BACKLOG = "todo.lektier_backlog"
LEGACY = "todo.lektier"          # tidligere samleliste - tømmes ved migrering
KEEP_BACKLOG_DONE_DAYS = 14
SUMMARY_MAX = 38                 # én linje i to-do-kortet på en telefon (~390 px)
VERSION = "4"   # hæves -> åbne/afkrydsede poster i dag-listerne genskabes i nyt format


def _items(entity: str) -> list[dict]:
    try:
        resp = outputs.call_service("todo", "get_items", {"entity_id": entity}, return_response=True) or {}
    except Exception as e:  # listen findes måske ikke (fx legacy slettet)
        log.debug("get_items %s fejlede: %s", entity, e)
        return []
    return (resp.get(entity) or {}).get("items", [])


def _hash(text: str) -> str:
    return hashlib.sha256((VERSION + text).encode()).hexdigest()[:16]


def summary(lesson: dict) -> str:
    abbr = lesson.get("abbr") or parser.abbreviate(lesson["subject"])
    text = parser.short_text(lesson["content"], max_len=max(20, SUMMARY_MAX - len(abbr) - 2))
    return f"{abbr}: {text or lesson['content'][:SUMMARY_MAX]}"


def slot_days(diary: dict, today: date) -> list[dict]:
    """De op til 5 første dage fra i dag, der har lektier (ikke kun Info)."""
    out = []
    for d in sorted(diary.get("days", []), key=lambda x: x["date"]):
        if d["date"] < today.isoformat():
            continue
        if any(l["subject"] != "Info" for l in d.get("lessons", [])):
            out.append(d)
        if len(out) == len(SLOTS):
            break
    return out


def _add(entity: str, summ: str, due: str | None, done: bool = False) -> str | None:
    """due=None for dag-listerne: dagen står i overskriften, så frist under hver linje er støj."""
    known = {i["uid"] for i in _items(entity)}
    data = {"entity_id": entity, "item": summ}
    if due:
        data["due_date"] = due
    outputs.call_service("todo", "add_item", data)
    new = [i for i in _items(entity) if i["uid"] not in known and i.get("summary") == summ]
    if not new:
        log.warning("Kunne ikke finde uid for ny post %r i %s", summ, entity)
        return None
    uid = new[0]["uid"]
    if done:
        outputs.call_service("todo", "update_item", {"entity_id": entity, "item": uid, "status": "completed"})
    return uid


def _remove(entity: str, uid: str) -> None:
    outputs.call_service("todo", "remove_item", {"entity_id": entity, "item": [uid]})


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def sync(diary: dict, today: date, state_file: Path) -> dict:
    state = _load(state_file)
    stats = {"added": 0, "moved": 0, "updated": 0, "removed": 0, "backlog": 0, "cleaned": 0}
    lists = {e: {i["uid"]: i for i in _items(e)} for e in SLOTS + [BACKLOG, LEGACY]}

    def find(entry):
        it = lists.get(entry.get("list"), {}).get(entry.get("uid"))
        return it

    # Ønsket placering: key -> (slot-liste, summary, dato, hash)
    wanted = {}
    for idx, d in enumerate(slot_days(diary, today)):
        for l in d["lessons"]:
            if l["subject"] == "Info":
                continue
            wanted[f"{d['date']}|{l['subject']}"] = (SLOTS[idx], summary(l), d["date"], _hash(l["content"]))

    # 1: eksisterende poster - passerede, flyttede, ændrede, fjernede
    for key, entry in list(state.items()):
        if entry.get("list") == BACKLOG:
            continue
        item = find(entry)
        # Listens status vinder: fjerner barnet et flueben, skal det også følge med ved flytning
        done = (item.get("status") == "completed") if item is not None else entry.get("done", False)
        if item is None and entry.get("list") in SLOTS and entry.get("uid"):
            # slettet manuelt i HA - genopret ikke (medmindre læreren ændrer teksten)
            entry.update(list=None, uid=None, deleted=True)
            continue
        if entry.get("deleted"):
            target = wanted.get(key)
            if target and target[3] != entry.get("hash"):
                entry.pop("deleted")          # læreren har ændret lektien -> vis den igen
            else:
                continue
        if entry["date"] < today.isoformat():
            if item:
                if not done:
                    _add(BACKLOG, item["summary"], entry["date"])
                    stats["backlog"] += 1
                _remove(entry["list"], entry["uid"])
            entry.update(list=BACKLOG if not done else None, uid=None, done=done)
            continue
        target = wanted.get(key)
        if target is None:  # fjernet af lærer, eller dagen er røget ud af de 5
            if item:
                _remove(entry["list"], entry["uid"])
                stats["removed"] += 1
            if done:
                entry.update(list=None, uid=None, done=True)  # husk afkrydsning hvis den kommer igen
            else:
                state.pop(key)
            continue
        slot, summ, due, h = target
        if entry.get("list") != slot or entry.get("v") != VERSION or item is None:
            if item:
                _remove(entry["list"], entry["uid"])
            entry.update(list=slot, uid=_add(slot, summ, None, done), hash=h, done=done, v=VERSION)
            stats["moved"] += 1
        elif entry.get("hash") != h and item and not done:
            outputs.call_service("todo", "update_item",
                                 {"entity_id": slot, "item": entry["uid"], "rename": summ})
            entry["hash"] = h
            stats["updated"] += 1

    # 2: nye lektier
    for key, (slot, summ, due, h) in wanted.items():
        if key in state:
            continue
        state[key] = {"list": slot, "uid": _add(slot, summ, None), "hash": h, "date": due, "done": False, "v": VERSION}
        stats["added"] += 1

    # 3: oprydning - gamle afkrydsede i backlog
    cutoff = (today - timedelta(days=KEEP_BACKLOG_DONE_DAYS)).isoformat()
    old = [uid for uid, i in lists[BACKLOG].items()
           if i.get("status") == "completed" and i.get("due") and i["due"] < cutoff]
    if old:
        outputs.call_service("todo", "remove_item", {"entity_id": BACKLOG, "item": old})
        stats["cleaned"] += len(old)

    cutoff = (today - timedelta(days=30)).isoformat()
    state = {k: v for k, v in state.items() if v.get("date", "") >= cutoff}
    state_file.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    log.info("To-do: %s", ", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats


def migrate_legacy(state_file: Path) -> int:
    """Tøm den gamle samleliste todo.lektier og glem dens poster, så de oprettes i dag-listerne.
    Afkrydsninger bevares som 'done' i tilstanden."""
    state = _load(state_file)
    legacy = {i["uid"]: i for i in _items(LEGACY)}
    n = 0
    for key, entry in list(state.items()):
        if entry.get("list") == LEGACY:
            item = legacy.get(entry.get("uid"))
            done = bool(item and item.get("status") == "completed")
            if done:
                state[key] = {"list": None, "uid": None, "hash": "", "date": entry["date"], "done": True}
            else:
                state.pop(key)
            n += 1
    if legacy:
        outputs.call_service("todo", "remove_item", {"entity_id": LEGACY, "item": list(legacy)})
    state_file.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    if n or legacy:
        log.info("Migrerede %d poster fra %s", len(legacy), LEGACY)
    return n

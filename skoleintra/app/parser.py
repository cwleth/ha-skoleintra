"""Parsere. Ugeplanen ligger som JSON i #root-attributten
data-clientlogic-settings-WeeklyPlansApp - ikke i HTML-strukturen."""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import unquote

from bs4 import BeautifulSoup

PREFIX = "data-clientlogic-settings-"


class ParseError(Exception):
    pass


def iso_week(d: date) -> str:
    """'46-2026'. ISO-uge, i modsætning til referencebibliotekets beregning."""
    y, w, _ = d.isocalendar()
    return f"{w:02d}-{y}"


# NB: SkoleIntra sætter "item" direkte efter barnets navn uden skråstreg,
# fx /parent/<id>/<barn>/item/weeklyplansandhomework/... (verificeret 11-11-2026).
def weekly_plan_path(child_url: str, d: date) -> str:
    return f"{child_url}item/weeklyplansandhomework/item/class/{iso_week(d)}"


def diary_path(child_url: str, note_id: str | None = None) -> str:
    base = f"{child_url}item/weeklyplansandhomework/diary"
    return f"{base}/notes/{note_id}" if note_id else base


def client_settings(html: str) -> dict:
    """Alle data-clientlogic-settings-*-blokke på siden (ikke kun #root), nøgle i
    lowercase. lxml lowercaser attributnavne: 'WeeklyPlansApp' -> 'weeklyplansapp'."""
    out: dict = {}
    for el in BeautifulSoup(html, "lxml").find_all(True):
        for attr, val in el.attrs.items():
            if attr.lower().startswith(PREFIX):
                key = attr[len(PREFIX):].lower()
                try:
                    out[key] = json.loads(val)
                except (TypeError, ValueError):
                    out[key] = val
    return out


def describe(obj, depth: int = 0, max_depth: int = 4) -> list[str]:
    """Kompakt strukturbeskrivelse af JSON til loggen."""
    pad = "  " * depth
    if depth > max_depth:
        return [f"{pad}..."]
    if isinstance(obj, dict):
        lines = []
        for k, v in list(obj.items())[:40]:
            if isinstance(v, (dict, list)):
                kind = f"list[{len(v)}]" if isinstance(v, list) else "dict"
                lines.append(f"{pad}{k}: {kind}")
                lines += describe(v, depth + 1, max_depth)
            else:
                lines.append(f"{pad}{k}: {str(v)[:90]!r}")
        return lines
    if isinstance(obj, list):
        lines = []
        for i, v in enumerate(obj[:2]):
            lines.append(f"{pad}[{i}]")
            lines += describe(v, depth + 1, max_depth)
        return lines
    return [f"{pad}{str(obj)[:90]!r}"]


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(unquote(raw), "lxml")
    # Kun blokelementer giver linjeskift - ellers brækker <b> ord midt over.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    text = soup.get_text("")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _lessons(items) -> list[dict]:
    out = []
    for item in items or []:
        content = html_to_text(item.get("Content"))
        if not content:
            continue
        subject = (item.get("Subject") or {}).get("FormattedTitle") or "Ukendt fag"
        out.append({"subject": subject.strip(), "content": content})
    return out


def parse_weekly_plan(html: str, requested_week: str) -> dict:
    data = client_settings(html).get("weeklyplansapp")
    if not isinstance(data, dict):
        raise ParseError("Ingen WeeklyPlansApp-data på siden - layoutet kan være ændret.")
    selected = data.get("SelectedPlan") or {}
    days = []
    for d in selected.get("DailyPlans") or []:
        days.append(
            {
                "date": d.get("Date"),
                "label": d.get("LongFormattedDate") or d.get("FormattedDate") or d.get("Day"),
                "lessons": _lessons(d.get("LessonPlans")),
            }
        )
    return {
        "week": requested_week,
        "site_week": data.get("SelectedWeek"),  # til verifikation af ugenummer
        "general": _lessons((selected.get("GeneralPlan") or {}).get("LessonPlans")),
        "days": days,
    }


def flatten(plan: dict) -> dict[str, str]:
    out = {}
    for lesson in plan.get("general", []):
        out[f"Ugen / {lesson['subject']}"] = lesson["content"]
    for day in plan.get("days", []):
        for lesson in day.get("lessons", []):
            out[f"{day.get('label') or day.get('date')} / {lesson['subject']}"] = lesson["content"]
    return out


def diff(old: dict, new: dict) -> list[str]:
    o, n = flatten(old), flatten(new)
    lines = []
    for k in sorted(set(n) - set(o)):
        lines.append(f"NY  {k}: {n[k].replace(chr(10), ' ')[:120]}")
    for k in sorted(set(o) & set(n)):
        if o[k] != n[k]:
            lines.append(f"ÆNDRET  {k}: {n[k].replace(chr(10), ' ')[:120]}")
    for k in sorted(set(o) - set(n)):
        lines.append(f"FJERNET  {k}")
    return lines


def summarize(plan: dict) -> str:
    n = len(plan.get("general", [])) + sum(len(d["lessons"]) for d in plan.get("days", []))
    return f"Uge {plan['week'].split('-')[0]}: {n} punkter"[:255]


def plan_markdown(plan: dict) -> str:
    lines = [f"## Ugeplan uge {plan['week'].split('-')[0]}"]
    if plan.get("general"):
        lines.append("### Hele ugen")
        lines += [f"**{l['subject']}**: {l['content']}" for l in plan["general"]]
    for d in plan.get("days", []):
        if d["lessons"]:
            lines.append(f"### {d['label']}")
            lines += [f"**{l['subject']}**: {l['content']}" for l in d["lessons"]]
    return "\n\n".join(lines)

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


def weekly_plan_path(child_url: str, d: date) -> str:
    return f"{child_url}/item/weeklyplansandhomework/item/class/{iso_week(d)}"


def client_settings(html: str) -> dict:
    """Alle data-clientlogic-settings-*-blokke på #root, nøgle i lowercase.
    (lxml lowercaser attributnavne, så 'WeeklyPlansApp' bliver 'weeklyplansapp'.)"""
    root = BeautifulSoup(html, "lxml").find(id="root")
    out: dict = {}
    if root is None:
        return out
    for attr, val in root.attrs.items():
        if attr.lower().startswith(PREFIX):
            try:
                out[attr[len(PREFIX):].lower()] = json.loads(val)
            except (TypeError, ValueError):
                out[attr[len(PREFIX):].lower()] = val
    return out


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

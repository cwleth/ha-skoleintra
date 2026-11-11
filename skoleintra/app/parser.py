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
        block.insert(0, "\n")
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


# ---------------------------------------------------------------- lektiebog
# Verificeret mod skolens ForældreIntra 11-11-2026:
#   #sk-diary-notes-container > ul.sk-list > li > div.sk-white-box
#     <b>Onsdag, 18. nov. 2026:</b>
#     div.sk-user-input  (lærerens frie HTML, typisk tabel FAG | LEKTIER)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
DIARY_DATE = re.compile(r"(\d{1,2})\.\s*([a-zæøå]+)\.?\s*(\d{4})", re.I)
HEADER_WORDS = {"fag", "lektier", "lektie"}


def parse_danish_date(text: str) -> date | None:
    m = DIARY_DATE.search(text or "")
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower()[:3])
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _cell_text(cell) -> str:
    return html_to_text(str(cell).replace("%", "%25"))  # html_to_text unquoter - beskyt '%'


def _items_from_user_input(box) -> tuple[list[dict], str | None]:
    """Returnér (punkter, duks). Tabelrækker -> fag/tekst, øvrig tekst -> 'Info'."""
    items, duks = [], None
    tables = box.find_all("table")
    for table in tables:
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            subject = _cell_text(cells[0]).strip().rstrip(":").strip()
            text = "\n".join(t for t in (_cell_text(c) for c in cells[1:]) if t).strip()
            if subject.lower() in HEADER_WORDS:
                # Overskriftsrække - men lærere skriver nogle gange en note i den.
                note = "\n".join(
                    ln for ln in text.split("\n") if ln.strip().lower() not in HEADER_WORDS
                ).strip()
                if note:
                    items.append({"subject": "Info", "content": note})
                continue
            if subject.lower().startswith("duks"):
                duks = text or None
                continue
            if not text:
                if subject and len(cells) == 1:
                    items.append({"subject": "Info", "content": subject})
                continue  # fag uden lektie
            items.append({"subject": subject.title() if subject.isupper() else (subject or "Info"),
                          "content": text})
        table.decompose()
    rest = _cell_text(box).strip()
    if rest:
        items.insert(0, {"subject": "Info", "content": rest})
    return items, duks


def parse_diary(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(id="sk-diary-notes-container")
    if container is None:
        raise ParseError("Ingen #sk-diary-notes-container på siden - layoutet kan være ændret.")
    title_el = soup.find(string=re.compile(r"Lektiebog", re.I))
    days = []
    for li in container.select("ul.sk-list > li"):
        box = li.find(class_="sk-white-box") or li
        head = box.find("b")
        label = head.get_text(" ", strip=True).rstrip(":") if head else ""
        d = parse_danish_date(label)
        user_input = box.find(class_="sk-user-input")
        if d is None or user_input is None:
            continue
        items, duks = _items_from_user_input(user_input)
        days.append({"date": d.isoformat(), "label": label, "lessons": items, "duks": duks})
    days.sort(key=lambda x: x["date"])
    return {"title": title_el.strip() if title_el else "Lektiebog", "days": days}


def upcoming(diary: dict, today: date, horizon_days: int = 14) -> dict:
    """Kun dage fra i dag og frem (inden for horisonten) - det er det, der er handling på."""
    end = today.toordinal() + horizon_days
    days = [d for d in diary["days"]
            if today.isoformat() <= d["date"] and date.fromisoformat(d["date"]).toordinal() <= end]
    return {**diary, "days": days, "general": []}


def next_school_day(diary: dict, today: date, after_hour: bool) -> dict | None:
    """Næste dag med indhold. Efter skoletid (after_hour) springes i dag over."""
    for d in diary["days"]:
        if d["date"] > today.isoformat() or (d["date"] == today.isoformat() and not after_hour):
            if d["lessons"]:
                return d
    return None


def diary_markdown(diary: dict, max_days: int = 7) -> str:
    out = [f"## {diary.get('title') or 'Lektiebog'}"]
    shown = [d for d in diary["days"] if d["lessons"]][:max_days]
    if not shown:
        out.append("Ingen lektier registreret.")
    for d in shown:
        out.append(f"### {d['label']}")
        out += [f"**{l['subject']}**: {l['content']}" for l in d["lessons"]]
    return "\n\n".join(out)


def diary_ids(html: str) -> list[str]:
    """Dagbogs-ID'er fra listesiden (.../diary/120 eller .../diary/notes/120)."""
    ids = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        m = re.search(r"/diary/(?:notes/)?(\d+)(?:/|$)", a["href"])
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


# ---------------------------------------------------------------- korte linjer
ABBR = {
    "matematik": "Mat", "dansk": "Dan", "engelsk": "Eng", "engels": "Eng", "tysk": "Tysk",
    "skoleskak": "Skak", "billedkunst": "Billed", "natur/teknologi": "N/T", "historie": "Hist",
    "kristendom": "Krist", "kristendomskundskab": "Krist", "idræt": "Idræt", "musik": "Musik",
    "håndværk og design": "H&D", "madkundskab": "Mad", "info": "Info",
}
SHORT_MAX = 70
_DROP = re.compile(r"^(lektie til i ?dag:?|man kan tilmelde sig.*:|https?://\S+|[_.:\-\s]+)$", re.I)
_PAGE = re.compile(r"(?<!\w)(s\.?\s*\d|side|sider|sæt|opg\.?|opgave|kapitel|book|bogen|hæfte)", re.I)
_PREFIX = re.compile(
    r"^(lektie til i ?dag:?\s*|bemærk\s+|til i ?dag\s+|(til i ?dag )?skal i blive færdige med( to sider)?:?\s*"
    r"|i skal blive færdige med( to sider)?:?\s*)+", re.I)
_SUFFIX = re.compile(r"\s*(skal være( lavet)? færdig\.?|skal være lavet færdig\.?)\s*$", re.I)


def abbreviate(subject: str) -> str:
    s = (subject or "").strip()
    return ABBR.get(s.lower(), s if len(s) <= 6 else s[:5] + ".")


def _clean(line: str) -> str:
    line = re.sub(r"\s*\([^)]*\)", "", line)            # (parenteser) ud
    line = _PREFIX.sub("", line).strip()
    line = _SUFFIX.sub("", line).strip()
    line = re.sub(r"\bside\b", "s.", line, flags=re.I)
    line = re.sub(r"\s+\.\s+", " · ", line)             # "Topic Book . Læs" -> "Topic Book · Læs"
    line = re.sub(r"^(lektie i|lektie til|lektien er)\s+", "", line, flags=re.I)
    line = re.sub(r"\s*\btil (næste uge|i ?dag|i morgen)\b", "", line, flags=re.I)  # dagen står i overskriften
    line = re.sub(r"\bs (?=\d)", "s. ", line)                                      # "s 22" -> "s. 22"
    line = re.sub(r"(\d)\s*(?:\+|og)\s*(\d)", r"\1+\2", line)                     # "5 + 6", "22 og 23" -> "5+6"
    line = re.sub(r"\s+samt\s+", " + ", line, flags=re.I)
    line = re.sub(r"\s{2,}", " ", line).strip(" .")
    if line.isupper():
        line = line.capitalize()
    return line[:1].upper() + line[1:] if line else line


def _score(line: str) -> int:
    return (3 if _PAGE.search(line) else 0) + (1 if re.search(r"\d", line) else 0) + (1 if line.lower().startswith("husk") else 0)


def short_text(content: str, max_len: int = SHORT_MAX) -> str:
    """Én til to linjers essens: de linjer med sidetal/opgaver, så korte som muligt."""
    lines = [ln.strip() for ln in (content or "").split("\n")]
    lines = [ln for ln in lines if ln and not _DROP.match(ln)]
    if not lines:
        return ""
    best = max(_score(ln) for ln in lines)
    picked = [_clean(ln) for ln in lines if _score(ln) == best]
    out = ""
    for p in picked:
        if not p:
            continue
        cand = f"{out} · {p}" if out else p
        if len(cand) > max_len and out:
            break
        out = cand
    if len(out) > max_len and " · " in out:
        segs = out.split(" · ")  # behold første led + led med tal (sidetal, sæt)
        out = " · ".join([segs[0]] + [x for x in segs[1:] if re.search(r"\d", x)])
    if len(out) > max_len:
        first = re.split(r"(?<=[a-zæøå0-9]{2})\.\s", out, maxsplit=1)[0]  # første sætning (ikke "s. 17")
        out = first if len(first) <= max_len else out[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return out


def add_short(diary: dict) -> dict:
    for d in diary.get("days", []):
        for l in d.get("lessons", []):
            l["abbr"] = abbreviate(l["subject"])
            l["short"] = short_text(l["content"]) or l["content"][:SHORT_MAX]
    return diary

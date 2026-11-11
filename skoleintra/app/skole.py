"""Skole-overblik: opslag, opslagstavle og kalender -> "Skal handles på" / "Godt at vide".

GDPR-regler (Christians krav):
- Indhold behandles kun i hukommelsen. Kun udtræk gemmes: dato, kanal, kort emne, handlingslinje, frist.
- Ingen vedhæftninger hentes, ingen links følges, ingen fulde tekster gemmes eller logges.
- Navne fjernes (scrub) fra emne og handlingslinje: ord med stort begyndelsesbogstav midt i en sætning
  erstattes, handlingslinjen er kun et udsnit omkring nøgleordet, og URL'er/e-mails/telefonnumre fjernes.
- Sortering med lokale regler (ingen AI/cloud).
- Notifikations-endpoints røres ikke (de peger kun på beskeder/opslag, og seen-endpoints må ikke kaldes).
- Beskeder indgår ikke endnu (afventer test af læsestatus).
- Loggen får kun antal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

from . import parser
from .client import SkoleIntra

log = logging.getLogger("skoleintra.skole")

PAUSE_S = 3
MAX_NEW_ARTICLES = 8
MAX_NEW_MESSAGES = 6
STATE_V = 3
LINE_MAX = 160
CAL_DAYS = 21
KEEP_DAYS = 45
MAX_HANDLE, MAX_INFO = 20, 30

ACTION = re.compile(
    r"\b(frist\w*|senest|medbring\w*|tilmeld\w*|husk\w*|aflever\w*|betal\w*|deadline|samtykke|"
    r"svar\s+(?:p\xe5|inden|senest)|udfyld\w*|sig\s+til)\b", re.I)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "okt": 10, "nov": 11, "dec": 12}
DATE_TXT = re.compile(r"\b(\d{1,2})\.?\s*(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-z\xe6\xf8\xe5]*", re.I)
DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
ALLOW = {w.lower() for w in (
    "DUKSE SkoleIntra ForældreIntra SFO "
    "Mandag Tirsdag Onsdag Torsdag Fredag Lørdag Søndag "
    "Januar Februar Marts April Maj Juni Juli August September Oktober November December "
    "Jan Feb Mar Apr Jun Jul Aug Sep Okt Nov Dec Dansk Matematik Engelsk Tysk Idræt Musik").split()}
URL = re.compile(r"(https?://\S+|www\.\S+|\S+@\S+)")
PHONE = re.compile(r"\+?\d[\d ]{6,}\d")


def scrub(text: str, limit: int = 120) -> str:
    """Fjern URL'er, e-mails, telefonnumre og egennavne (stort bogstav midt i sætning)."""
    t = PHONE.sub("", URL.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    out, start = [], True
    for w in t.split(" "):
        core = re.sub(r"^\W+|\W+$", "", w)
        if (core and core[0].isupper() and not start and not core.isupper()
                and core.lower() not in ALLOW):
            w = w.replace(core, "…")
        out.append(w)
        start = w.endswith((".", "!", "?", ":"))
    s = re.sub(r"(…\s*){2,}", "… ", " ".join(out)).strip()
    return s[:limit].rstrip()


def find_date(text: str, today: date) -> date | None:
    cands = []
    for m in DATE_TXT.finditer(text):
        cands.append((m.start(), int(m.group(1)), MONTHS[m.group(2).lower()[:3]]))
    for m in DATE_NUM.finditer(text):
        cands.append((m.start(), int(m.group(1)), int(m.group(2))))
    for _, d, mo in sorted(cands):
        try:
            c = date(today.year, mo, d)
        except ValueError:
            continue
        if c < today - timedelta(days=30):
            c = date(today.year + 1, mo, d)
        return c
    return None


def action_lines(text: str, today: date, max_lines: int = 2) -> list[dict]:
    """Udsnit (~7 ord før/efter nøgleordet) pr. sætning med handlingsord. Dato søges i hele sætningen.
    Slutter sætningen med ':' følger en liste (fx 'skal medbringe:'): de næste op til 3 linjer tages med."""
    out = []
    parts = [x for x in re.split(r"(?<=[.!?])\s+(?=[A-Z\xc6\xd8\xc5])|\n+", text) if x.strip()]
    for i, sent in enumerate(parts):
        m = ACTION.search(sent)
        if not m:
            continue
        words = sent.split()
        pos = len(sent[:m.start()].split())
        window = " ".join(words[max(0, pos - 7): pos + 8])
        scope = sent
        if sent.rstrip().endswith(":"):
            follow = [x.strip()[:45] for x in parts[i + 1:i + 4]]
            # punktum mellem listepunkter: hvert punkt tæller som sætningsstart i scrub (punkter er ting, ikke navne)
            window += " " + ". ".join(follow)
            scope += " " + " ".join(follow)
        due = find_date(scope, today)
        out.append({"line": scrub(window, LINE_MAX), "due": due.isoformat() if due else None})
        if len(out) >= max_lines:
            break
    return out


def _eid(channel: str, raw_id: str) -> str:
    return hashlib.sha1(f"{channel}:{raw_id}".encode()).hexdigest()[:10]


def entries_from_text(channel: str, raw_id: str, title: str, text: str, published: date, today: date) -> list[dict]:
    topic = scrub(title, 60) or channel
    acts = action_lines(text, today)
    if not acts:
        return [{"id": _eid(channel, raw_id), "channel": channel, "date": published.isoformat(),
                 "topic": topic, "action": None, "due": None, "kind": "info"}]
    return [{"id": _eid(channel, f"{raw_id}:{i}"), "channel": channel, "date": published.isoformat(),
             "topic": topic, "action": a["line"], "due": a["due"], "kind": "handle"} for i, a in enumerate(acts)]


# ---------------------------------------------------------------- hentning
def _get(si: SkoleIntra, path: str) -> str:
    return si.get(f"{si.child_url}/{path.lstrip('/')}").text


def _body_title(row) -> tuple[str, str]:
    body = "\n".join(parser.html_to_text(str(e)) for e in row.select(".sk-user-input"))
    t = row.select_one(".sk-news-item-content > .h-fnt-bd + div")
    title = t.get_text(" ", strip=True) if t else (body.split("\n")[0] if body else "")
    return title, body


def parse_when(text: str | None, today: date) -> date:
    """Dato fra SkoleIntras tekster: '10. nov. 2026', '10. nov.', 'i går', 'for 2 dage siden'. Aldrig i fremtiden."""
    t = (text or "").strip()
    d = parser.parse_danish_date(t)
    if d is None and (iso := re.match(r"(\d{4})-(\d{2})-(\d{2})", t)):
        try:
            d = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            d = None
    low = t.lower()
    if d is None:
        if "i dag" in low:
            d = today
        elif "i g\xe5r" in low or "for en dag" in low or "for 1 dag" in low:
            d = today - timedelta(days=1)
        elif (m := re.search(r"for (\d+) dag", low)):
            d = today - timedelta(days=int(m.group(1)))
        else:
            d = find_date(t, today)
            if d and d > today:
                d = date(d.year - 1, d.month, d.day)
    return d or today


def _published(row, today: date) -> date:
    ts = row.select_one(".sk-news-item-header .sk-news-item-timestamp")
    return parse_when(ts.get_text(" ", strip=True) if ts else "", today)


def fetch_news(si: SkoleIntra, seen: set[str], today: date) -> tuple[list[dict], set[str]]:
    new_seen, entries = set(), []
    soup = BeautifulSoup(_get(si, "news/news"), "lxml")
    ids = []
    for a in soup.select(".sk-l-content a[href]"):
        m = re.search(r"/news/news/(\d+)$", a["href"])
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    todo = [i for i in ids if _eid("opslag", i + ":0") not in seen and _eid("opslag", i) not in seen][:MAX_NEW_ARTICLES]
    log.info("Opslag: %d i listen, %d nye hentes", len(ids), len(todo))
    for nid in todo:
        time.sleep(PAUSE_S)
        art = BeautifulSoup(_get(si, f"news/news/{nid}"), "lxml")
        row = art.select_one(".sk-news-item") or art.select_one(".sk-l-content")
        if row is None:
            continue
        title, body = _body_title(row)
        es = entries_from_text("opslag", nid, title, body, _published(row, today), today)
        entries += es
        new_seen.update(e["id"] for e in es)
        new_seen.add(_eid("opslag", nid))
    return entries, new_seen


def fetch_pins(si: SkoleIntra, seen: set[str], today: date) -> tuple[list[dict], set[str]]:
    soup = BeautifulSoup(_get(si, "news/pins"), "lxml")
    entries, new_seen = [], set()
    rows = soup.select(".sk-news-item")
    for row in rows:
        pid = row.get("data-feed-item-id")
        if not pid or _eid("opslagstavle", pid) in seen or _eid("opslagstavle", pid + ":0") in seen:
            continue
        title, body = _body_title(row)
        es = entries_from_text("opslagstavle", pid, title, body, _published(row, today), today)
        entries += es
        new_seen.update(e["id"] for e in es)
        new_seen.add(_eid("opslagstavle", pid))
    log.info("Opslagstavle: %d i alt, %d nye", len(rows), len(entries))
    return entries, new_seen


def _init_json(html: str, name: str) -> dict:
    marker = f"NewSkoleIntra.{name}.Init("
    i = html.find(marker)
    if i < 0:
        return {}
    j = html.find("{", i + len(marker))
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[j:])
        return obj
    except ValueError:
        return {}


def _ms(v: str | None) -> datetime | None:
    m = re.search(r"/Date\((-?\d+)", v or "")
    return datetime.fromtimestamp(int(m.group(1)) / 1000) if m else None


def fetch_calendar(si: SkoleIntra, today: date) -> list[dict]:
    """Skole- og aktivitetskalender de næste 21 dage. Skema (Lessons) og fødselsdage (type 6) springes over."""
    cfg = _init_json(_get(si, "calendar/myCalendar"), "Calendar")
    sources = (cfg.get("CalendarModel") or {}).get("EventSources") or []
    a = int(datetime.combine(today, datetime.min.time()).timestamp())
    b = a + CAL_DAYS * 86400
    out, seen_ids = [], set()
    for src in sources:
        if src.get("Name") not in ("School", "Activity") or src.get("RequestType", "GET") != "GET":
            continue
        time.sleep(PAUSE_S)
        sep = "&" if "?" in src["Url"] else "?"
        url = src["Url"]
        url = url[len(si.base):] if url.startswith(si.base) else url
        r = si.get(f"{url.lstrip('/')}{sep}start={a}&end={b}")
        try:
            events = r.json()
        except ValueError:
            log.warning("Kalender %s: svar var ikke JSON", src.get("Name"))
            continue
        for e in events if isinstance(events, list) else []:
            key = f"{e.get('type')}:{e.get('originalId')}"
            title = e.get("title") or ""
            if key in seen_ids or e.get("type") == 6 or re.search(r"f\xf8dselsdag", title, re.I):
                continue
            seen_ids.add(key)
            start = _ms(e.get("startDate"))
            if start is None and e.get("start"):
                try:
                    start = datetime.fromisoformat(str(e["start"])[:19])
                except ValueError:
                    pass
            if start is None:
                continue
            kind = "handle" if ACTION.search(title) else "info"
            out.append({"id": _eid("kalender", key), "channel": "kalender", "date": start.date().isoformat(),
                        "topic": scrub(title, 60), "action": scrub(title) if kind == "handle" else None,
                        "due": start.date().isoformat(), "kind": kind})
    log.info("Kalender: %d begivenheder de næste %d dage", len(out), CAL_DAYS)
    return out


def fetch_messages(si: SkoleIntra, seen: set[str], today: date, stored: dict) -> tuple[list[dict], set[str]]:
    """Beskeder fra listen (emne, dato, ulæst) + brødtekst af seneste besked i nye tråde.
    Verificeret 11-11-2026: hentning af en tråd markerer den IKKE som læst. Afsendere/modtagere læses ikke."""
    from . import msgprobe

    soup = BeautifulSoup(_get(si, "messages/conversations"), "lxml")
    el = soup.find(attrs={msgprobe.ATTR: True})
    try:
        convs = (json.loads(el[msgprobe.ATTR]).get("Conversations") or []) if el is not None else []
    except ValueError:
        convs = []
    entries, new_seen, bodies = [], set(), 0
    for c in convs:
        lm = c.get("LatestMessageId")
        if not lm:
            continue
        key = str(c.get("ThreadId") or lm)
        eid = _eid("besked", f"{key}:{lm}")
        unread = bool(c.get("IsUnread"))
        when = parse_when(str(c.get("Date") or ""), today)
        if eid in stored:                       # kendt: opdatér kun ulæst-status
            stored[eid]["unread"] = unread
            continue
        if eid in seen:                         # allerede behandlet (og siden udløbet)
            continue
        if not unread and when < today - timedelta(days=21):
            continue
        if bodies >= MAX_NEW_MESSAGES:
            continue                              # resten tages næste kørsel
        bodies += 1
        time.sleep(PAUSE_S)
        try:
            if c.get("ThreadId"):
                msgs = msgprobe._ajax_get(si, "messages/conversations/loadmessagesforselectedconversation", {
                    "threadId": c["ThreadId"], "takeFromRootMessageId": str(lm), "takeToMessageId": "0", "searchRequest": ""})
            else:
                msgs = msgprobe._ajax_get(si, "messages/conversations/getmessageforthreadlessconversation", {"messageId": str(lm)})
            latest = max((m for m in msgs if isinstance(m, dict)), key=lambda m: int(m.get("Id") or 0))
            body = parser.html_to_text(latest.get("BaseText"))   # kun seneste besked, ikke citerede tidligere
        except Exception as e:  # en enkelt tråd må ikke vælte resten
            log.warning("Besked: tråd sprunget over (%s)", e.__class__.__name__)
            continue
        subject = str(c.get("Subject") or "")
        acts = action_lines(body, today, 1) or action_lines(subject, today, 1)
        due = (acts[0]["due"] if acts else None) or (find_date(subject, today).isoformat() if find_date(subject, today) else None)
        entries.append({"id": eid, "channel": "besked", "date": when.isoformat(), "topic": scrub(subject, 60) or "Besked",
                        "action": acts[0]["line"] if acts else None, "due": due,
                        "kind": "handle" if acts else "info", "unread": unread})
        new_seen.add(eid)
    log.info("Beskeder: %d i listen, %d ulæste, %d nye hentet", len(convs), sum(bool(c.get("IsUnread")) for c in convs), len(entries))
    return entries, new_seen


# ---------------------------------------------------------------- tilstand
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _relevant(e: dict, today: date) -> bool:
    if e.get("unread") and e["date"] >= (today - timedelta(days=KEEP_DAYS)).isoformat():
        return True                               # ulæste beskeder bliver, til de er læst
    ref = e.get("due") or e["date"]
    if e["kind"] == "handle":
        days = 7
    else:
        days = 1 if e["channel"] == "kalender" else 14   # opslag/beskeder er nyttige et par uger
    return ref >= (today - timedelta(days=days)).isoformat() and e["date"] >= (today - timedelta(days=KEEP_DAYS)).isoformat()


def run(si: SkoleIntra, today: date, state_file: Path) -> tuple[tuple[str, dict], list[dict]]:
    """Returnerer ((sensor-state, attributter), nye "skal handles"-poster). Første kørsel giver ingen nye."""
    st = _load(state_file)
    baseline = not st
    reset = bool(st) and st.get("v") != STATE_V      # datoparsing rettet: hent opslag igen én gang (uden push)
    if reset:
        st = {**st, "seen": [], "entries": [e for e in st.get("entries", []) if e["channel"] == "kalender"]}
    seen = set(st.get("seen", []))
    stored = {e["id"]: e for e in st.get("entries", [])}

    new: list[dict] = []
    for fn in (fetch_news, fetch_pins):
        time.sleep(PAUSE_S)
        es, ns = fn(si, seen, today)
        new += es
        seen |= ns
    time.sleep(PAUSE_S)
    msg_baseline = not st.get("msg_done")
    try:
        es, ns = fetch_messages(si, seen, today, stored)
    except Exception as e:  # beskeder må ikke vælte opslag og kalender
        log.warning("Beskeder fejlede: %s", e.__class__.__name__)
        es, ns = [], set()
    msg_new = es
    new += es
    seen |= ns
    time.sleep(PAUSE_S)
    cal = fetch_calendar(si, today)

    pushed = [e for e in new if e["kind"] == "handle" and e["id"] not in stored
              and not (e["channel"] == "besked" and msg_baseline)
              and not (reset and e["channel"] in ("opslag", "opslagstavle"))]   # første beskedsweep giver ikke push
    for e in new:
        log.info("  ny: %s %s dato=%s relevant=%s", e["channel"], e["kind"], e["date"], _relevant(e, today))
    for e in new:
        stored[e["id"]] = e
    for e in cal:
        if e["id"] not in stored:
            if e["kind"] == "handle":
                pushed.append(e)
            stored[e["id"]] = e
        else:
            stored[e["id"]].update({k: e[k] for k in ("date", "topic", "action", "due", "kind")})
    # kalender-poster der er forsvundet fra feedet (aflyst/ændret) fjernes
    cal_ids = {e["id"] for e in cal}
    stored = {i: e for i, e in stored.items() if e["channel"] != "kalender" or i in cal_ids}

    keep = [e for e in stored.values() if _relevant(e, today)]
    keep.sort(key=lambda e: (e.get("due") or e["date"], e["channel"]))
    handle = [e for e in keep if e["kind"] == "handle"][:MAX_HANDLE]
    info = sorted((e for e in keep if e["kind"] == "info"), key=lambda e: e["date"], reverse=True)[:MAX_INFO]

    state_file.write_text(json.dumps({"seen": sorted(seen), "entries": keep, "msg_done": True, "v": STATE_V}, ensure_ascii=False), "utf-8")
    log.info("Skole: %d skal handles på, %d godt at vide, %d nye handlingsposter%s",
             len(handle), len(info), len(pushed), " (baseline, ingen push)" if baseline else "")
    attrs = {"handle": handle, "info": info, "opdateret": datetime.now().astimezone().isoformat(timespec="seconds"),
             "kilder": "opslag, opslagstavle, beskeder, kalender"}
    return (str(len(handle)), attrs), ([] if baseline else pushed)


def push_text(items: list[dict]) -> str:
    lines = []
    for e in items[:8]:
        due = f" (senest {e['due'][8:]}/{e['due'][5:7]})" if e.get("due") else ""
        lines.append(f"- {e['topic']}: {e['action']}{due}")
    return "\n".join(lines)

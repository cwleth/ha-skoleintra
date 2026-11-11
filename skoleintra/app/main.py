"""Entrypoint for add-on'en. Mode vælges i konfigurationen:

  discover  - log ind, dump sider til /share/skoleintra_dump, afslut
  dry-run   - hent og parse, skriv til loggen, rør ikke HA
  once      - én fuld kørsel (sensor + evt. notifikation), afslut
  schedule  - kør på tidspunkterne i run_at, bliv kørende
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from pathlib import Path

from bs4 import BeautifulSoup

from . import ai, outputs, parser, todo_sync
from .client import LoginBlocked, LoginRejected, SkoleIntra, SkoleIntraError, UnsupportedLogin

DATA = Path(os.environ.get("SI_DATA_DIR", "/data"))
OPTIONS = Path(os.environ.get("SI_OPTIONS_FILE", DATA / "options.json"))
log = logging.getLogger("skoleintra")


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_client(o: dict) -> SkoleIntra:
    return SkoleIntra(o["base_url"], o["username"], o["password"], DATA, o.get("child_url"))


def target_date(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=7) if today.weekday() >= 5 else today  # weekend -> næste uge


# ---------------------------------------------------------------- discover
DATE_RE = re.compile(r"^\w+dag, \d{1,2}\. \w+\.? \d{4}:?$")


def _label(el) -> str:
    cls = ".".join(el.get("class", []))
    ident = f"#{el['id']}" if el.get("id") else ""
    attrs = " ".join(f"{k}" for k in el.attrs if k.startswith("data-"))
    return f"<{el.name}{ident}{'.' + cls if cls else ''}{' ' + attrs if attrs else ''}>"


def dom_skeleton(soup) -> list[str]:
    """HTML-struktur omkring de to første dato-overskrifter, til at bygge parseren."""
    heads = [s for s in soup.find_all(string=DATE_RE)][:2]
    if not heads:
        return ["--- skeleton: ingen dato-overskrift fundet"]
    out = ["--- skeleton: forfædre til første dato ---"]
    el = heads[0].parent
    chain = [el] + list(el.parents)[:8]
    out += [f"  {'  ' * i}{_label(a)}" for i, a in enumerate(reversed(chain))]
    # Find laveste fælles forfader til de to datoer = listen; første barn = én dag.
    block = heads[0].parent
    if len(heads) == 2:
        second = set(id(p) for p in heads[1].parents)
        for p in heads[0].parents:
            if id(p) in second:
                common = p
                break
        else:
            common = None
        if common is not None:
            for child in common.children:
                if getattr(child, "name", None) and heads[0] in child.find_all(string=DATE_RE):
                    block = child
                    break
            out.append(f"--- skeleton: liste-container {_label(common)}")
    out.append(f"--- skeleton: én dag {_label(block)} ---")

    def walk(node, depth):
        if depth > 9 or len(out) > 220:
            return
        for c in node.children:
            if getattr(c, "name", None):
                own = "".join(t for t in c.find_all(string=True, recursive=False)).strip()
                out.append(f"  {'  ' * depth}{_label(c)} {own[:40]!r}" if own else f"  {'  ' * depth}{_label(c)}")
                walk(c, depth + 1)

    walk(block, 0)
    return out


def discover(o: dict) -> int:
    out = Path(o.get("dump_dir") or "/share/skoleintra_dump")
    out.mkdir(parents=True, exist_ok=True)
    si = make_client(o)
    summary = []

    def dump(name: str, path: str) -> str | None:
        try:
            r = si.get(path)
        except SkoleIntraError:
            raise
        except Exception as e:
            summary.append(f"{name}: FEJL {e}")
            return None
        (out / f"{name}.html").write_text(r.text, "utf-8")
        settings = parser.client_settings(r.text)
        for k, v in settings.items():
            save_json(out / f"{name}.{k}.json", v)
        summary.append(
            f"{name}: HTTP {r.status_code}, {len(r.text)} tegn, url={r.url}, "
            f"settings={sorted(settings) or 'ingen'}"
        )
        return r.text

    landing = si.ensure_child_url()
    if landing is None:  # genbrugt session - hent forsiden for at se hvor vi lander
        landing = si.get("")
    (out / "landing.html").write_text(landing.text, "utf-8")
    summary.append(f"child_url: {si.child_url}")
    summary.append(f"landing: HTTP {landing.status_code}, url={landing.url}")

    index_html = dump("index", f"{si.child_url}/Index") or landing.text

    # Alle links på forsiden til loggen, så stierne kan læses uden adgang til /share.
    host = urlsplit(landing.url).netloc
    seen_links = []
    for a in BeautifulSoup(landing.text + (index_html or ""), "lxml").find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if href.startswith("http") and urlsplit(href).netloc != host:
            continue
        entry = f"  {a.get_text(' ', strip=True)[:50]} -> {urlsplit(href).path}"
        if entry not in seen_links:
            seen_links.append(entry)
    summary.append(f"links ({len(seen_links)}):")
    summary += seen_links[:120]
    week = parser.iso_week(target_date())
    dump("weeklyplan", parser.weekly_plan_path(si.child_url, target_date()))
    summary.append(f"Anmodet uge: {week}")

    # Dagbog ("Lektier"): listesiden + den kendte note. Tekst og links i loggen,
    # så strukturen kan læses uden adgang til /share.
    (out / "links.txt").write_text("\n".join(seen_links), "utf-8")
    pages = [("diary", parser.diary_path(si.child_url))]
    if o.get("diary_id"):
        pages.append(("diary_note", parser.diary_path(si.child_url, o["diary_id"])))
    for name, path in pages:
        html = dump(name, path)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        main_el = soup.find("main") or soup.find(id="root") or soup.body or soup
        for tag in main_el.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\n\s*\n+", "\n", main_el.get_text("\n", strip=True))
        summary.append(f"--- {name}: tekst (første 2500 tegn) ---")
        summary.append(text[:2500])
        sub = []
        for a in main_el.find_all("a", href=True):
            path_only = urlsplit(a["href"]).path
            if "weeklyplansandhomework" in path_only and path_only not in sub:
                sub.append(path_only)
        summary.append(f"--- {name}: lektie-links ({len(sub)}) ---")
        summary += [f"  {x}" for x in sub[:40]]
        classes = sorted({c for el in main_el.find_all(class_=True) for c in el["class"] if c.startswith("sk-")})
        summary.append(f"--- {name}: sk-klasser: {classes[:60]}")
        for key, val in parser.client_settings(html).items():
            summary.append(f"--- {name}: JSON {key} ---")
            summary += parser.describe(val)

    (out / "summary.txt").write_text("\n".join(summary), "utf-8")
    print("\n".join(summary))
    print(f"\nDump skrevet til {out}")
    return 0


# ---------------------------------------------------------------- normal kørsel
def resolve_diary(si: SkoleIntra, o: dict) -> tuple[str, str]:
    """Hent lektiebogen. Prøv det konfigurerede/gemte ID; virker det ikke, find
    ID'et på listesiden (klassen skifter typisk dagbog ved skoleårets start)."""
    state = load_json(DATA / "diary.json", {})
    candidates = [x for x in (o.get("diary_id"), state.get("diary_id")) if x]
    for did in dict.fromkeys(candidates):
        r = si.get(parser.diary_path(si.child_url, did))
        if r.status_code == 200 and "sk-diary-notes-container" in r.text:
            return did, r.text
        log.warning("Dagbog %s svarede ikke som forventet (HTTP %s) - leder efter ny", did, r.status_code)
    listing = si.get(parser.diary_path(si.child_url))
    ids = parser.diary_ids(listing.text)
    if not ids:
        raise parser.ParseError("Ingen lektiebog fundet på siden 'Lektier'.")
    did = ids[0]
    r = si.get(parser.diary_path(si.child_url, did))
    save_json(DATA / "diary.json", {"diary_id": did})
    log.info("Bruger lektiebog %s", did)
    return did, r.text


def summarize_diary(diary: dict) -> str:
    n = sum(len(d["lessons"]) for d in diary["days"])
    return f"{n} punkter de næste {len(diary['days'])} skoledage"[:255]


def run_once(o: dict, dry_run: bool = False) -> int:
    si = make_client(o)
    si.ensure_child_url()
    now = datetime.now()
    today = now.date()
    did, html = resolve_diary(si, o)
    diary = parser.add_short(parser.upcoming(parser.parse_diary(html), today))
    nxt = parser.next_school_day(diary, today, after_hour=now.hour >= 14)

    condensed = None
    if o.get("anthropic_api_key") and diary["days"]:
        condensed = ai.condense(diary, o["anthropic_api_key"], o["ai_model"], DATA / "ai_cache.json")
    markdown = ai.to_markdown(condensed, diary["title"]) if condensed else parser.diary_markdown(diary)
    next_md = parser.diary_markdown({"title": "Næste skoledag", "days": [nxt]}) if nxt else "Ingen lektier registreret."

    previous = load_json(DATA / "last_diary.json", {})
    # Kun fremtidige dage i begge, så dage der ruller af ikke ligner ændringer.
    prev_cut = {**previous, "days": [d for d in previous.get("days", []) if d["date"] >= today.isoformat()]}
    changes = parser.diff(prev_cut, diary) if previous else []

    if dry_run:
        print(json.dumps(diary, ensure_ascii=False, indent=2))
        print("\n--- MARKDOWN ---\n" + markdown)
        print("\n--- NÆSTE SKOLEDAG ---\n" + next_md)
        print("\n--- ÆNDRINGER ---\n" + ("\n".join(changes) or "(ingen)"))
        return 0

    outputs.publish({
        "lektier": (summarize_diary(diary), {**diary, "changes": changes, "condensed": condensed,
                                             "markdown": markdown, "diary_id": did,
                                             "slots": [d["date"] for d in todo_sync.slot_days(diary, today)]}),
        "naeste_skoledag": ((nxt["label"] if nxt else "Ingen lektier"),
                            {"date": nxt["date"] if nxt else None,
                             "lessons": nxt["lessons"] if nxt else [],
                             "duks": nxt.get("duks") if nxt else None,
                             "markdown": next_md}),
        "status": ("ok", {"besked": f"Hentet lektiebog {did}", "tidspunkt": now_iso()}),
        "sidst_hentet": (now_iso(), None),
    })
    if o.get("todo_sync", True):
        try:
            todo_sync.migrate_legacy(DATA / "todo_sync.json")
            todo_sync.sync(diary, today, DATA / "todo_sync.json")
        except Exception as e:  # to-do må ikke vælte sensorerne
            log.warning("To-do-synkronisering fejlede: %s", e)
    if changes:
        outputs.notify(o["notify_service"], "Lektier opdateret",
                       markdown if condensed else "\n".join(changes[:15]))
    save_json(DATA / "last_diary.json", diary)
    return 0


def report_failure(o: dict, e: Exception) -> None:
    status = getattr(e, "status", "fejl")
    msg = str(e) or e.__class__.__name__
    log.error("%s: %s", status, msg)
    try:
        outputs.publish({"status": (status, {"besked": msg, "tidspunkt": now_iso()})})
    except Exception as pe:
        log.warning("Kunne ikke publicere status: %s", pe)
    # Login-problemer kræver handling - notificér, men kun når status skifter.
    last = load_json(DATA / "last_status.json", {})
    if isinstance(e, (LoginRejected, LoginBlocked, UnsupportedLogin)) and last.get("status") != status:
        try:
            outputs.notify(o["notify_service"], "SkoleIntra: login stoppet", msg)
        except Exception as ne:
            log.warning("Kunne ikke sende notifikation: %s", ne)
    save_json(DATA / "last_status.json", {"status": status})


MIN_INTERVAL_S = 300  # forsvar mod løkker: aldrig to rigtige hentninger inden for 5 min


def too_soon() -> bool:
    last = load_json(DATA / "last_fetch.json", {}).get("ts", 0)
    return time.time() - last < MIN_INTERVAL_S


def safe_run(o: dict, dry_run: bool = False) -> int:
    if not dry_run:
        if too_soon():
            log.warning("Springer over: sidste hentning var for under %d min siden", MIN_INTERVAL_S // 60)
            return 0
        save_json(DATA / "last_fetch.json", {"ts": time.time()})
    try:
        rc = run_once(o, dry_run)
        if not dry_run:
            save_json(DATA / "last_status.json", {"status": "ok"})
        return rc
    except Exception as e:
        if dry_run:
            log.exception("Dry-run fejlede")
            return 1
        report_failure(o, e)
        if not isinstance(e, SkoleIntraError):
            log.exception("Uventet fejl")
        return 1


# ---------------------------------------------------------------- planlægning
def parse_times(spec: str) -> list[tuple[int, int]]:
    times = []
    for part in spec.split(","):
        h, m = part.strip().split(":")
        times.append((int(h), int(m)))
    if not times:
        raise ValueError("run_at er tom")
    return sorted(times)


def next_run(times: list[tuple[int, int]], now: datetime) -> datetime:
    for day in range(2):
        for h, m in times:
            t = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                return t
    raise RuntimeError("unreachable")


def schedule(o: dict) -> int:
    times = parse_times(o["run_at"])
    if not (DATA / "last_diary.json").exists():
        log.info("Ingen tidligere plan - kører med det samme")
        safe_run(o)
    while True:
        t = next_run(times, datetime.now())
        log.info("Næste kørsel %s", t.strftime("%d-%m-%Y %H:%M"))
        time.sleep(max(1, (t - datetime.now()).total_seconds()))
        safe_run(o)


# ---------------------------------------------------------------- main
def idle(rc: int) -> int:
    """Faste kørsler afslutter ikke: watchdog ville genstarte dem i en løkke."""
    log.info("Færdig (rc=%s). Skift mode til schedule for drift - venter i tomgang.", rc)
    while True:
        time.sleep(3600)


def main() -> int:
    o = load_json(OPTIONS, None)
    if o is None:
        print(f"Kan ikke læse {OPTIONS}")
        return 1
    logging.basicConfig(level=o.get("log_level", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # paho logger en falsk ERROR ved lukning efter publish.multiple; rigtige fejl kaster exceptions.
    logging.getLogger("paho.mqtt.client").setLevel(logging.CRITICAL)
    if not o.get("username") or not o.get("password"):
        log.error("username/password er ikke udfyldt. Stopper UDEN loginforsøg.")
        return 1

    if make_client(o).guard.handle_reset_option(bool(o.get("reset_login_guard"))):
        log.warning("Loginværnet er nulstillet. Slå reset_login_guard FRA igen.")

    mode = o.get("mode", "discover")
    log.info("Mode: %s", mode)
    # Discover-dumpet indeholder persondata - ryd det, så snart vi er i drift.
    dump_dir = Path(o.get("dump_dir") or "/share/skoleintra_dump")
    if mode != "discover" and dump_dir.exists():
        import shutil

        shutil.rmtree(dump_dir, ignore_errors=True)
        log.info("Slettede discover-dump %s (exists=%s)", dump_dir, dump_dir.exists())
    if mode == "discover":
        try:
            rc = discover(o)
        except SkoleIntraError as e:
            report_failure(o, e)
            rc = 1
        return IDLE(rc)
    if mode == "dry-run":
        return IDLE(safe_run(o, dry_run=True))
    if mode == "once":
        return IDLE(safe_run(o))
    if mode == "schedule":
        return schedule(o)
    log.error("Ukendt mode: %s", mode)
    return 1


IDLE = idle  # kan udskiftes i tests


if __name__ == "__main__":
    sys.exit(main())

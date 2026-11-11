"""mode: map - kortlæg sider til beskeder/opslag/notifikationer/kalender UDEN at læse indhold.

GDPR: Intet gemmes på disk. Loggen får kun struktur:
- menu-links (tekst fra navigation/header er UI-tekst; øvrige linktekster maskeres),
- pr. side: HTTP-status, endelig sti, nøgler i indlejrede JSON-indstillinger (værdier maskeret,
  undtagen stier/URL'er), og et DOM-skelet af hovedindholdet med maskeret tekst.
Maskering: alle bogstaver -> 'x'. Tal og tegnsætning bevares, så datoformater kan aflæses
('Onsdag, 11. nov' -> 'xxxxxx, 11. xxx'), men navne og indhold kan ikke.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import parser
from .client import SkoleIntraError

log = logging.getLogger("skoleintra.map")

KEYWORDS = re.compile(r"messag|besked|notif|news|nyhed|board|opslag|post|calendar|kalender|event|arrangement|signup|tilmeld", re.I)
# Kun oversigtssider under barnets sti: /parent/<id>/<barn>/<sektion>/<side>[/today]. Ingen tal-segmenter
# (enkeltbeskeder/-opslag), ingen filer (vedhæftninger som fødselsdagslister), intet under /Faelles eller /file.
OVERVIEW = re.compile(r"^/parent/\d+/[^/]+/[a-z]+/[a-zA-Z_]+(/today)?/?$")
MAX_PAGES = 6          # + forside = 7 opslag i alt; SkoleIntra har bot-beskyttelse
PAUSE_S = 3
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
# Kun linktekst inde i SkoleIntras egen menu og faneblade er UI-tekst; alt andet maskeres
UI_CONTAINERS = re.compile(r"^(sk-l-menu-container|sk-page-tabs-container|ccl-pagetabs)$")


def mask(text: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return _LETTERS.sub("x", t[:n])


def mask_path(path: str) -> str:
    """Behold rene ord-segmenter ('/parent/<id>/<barn>/messages/getinitailconversations', 'v1');
    maskér filnavne, id'er med bogstaver o.l. Querystring: navne beholdes, værdier maskeres.
    Barnets eget navn står i alle stier og er ikke tredjepersons data."""
    path, _, query = path.partition("?")
    segs = path.split("/")
    private = len(segs) > 2 and segs[1].lower() in ("file", "faelles")
    keep = lambda sg: (not private and "." not in sg and
                       re.fullmatch(r"[a-z_\-]+|[a-z]+\d+|\d+|[A-Z][a-z]+(item)?|[a-zA-Z]+|\{\d+\}", sg or "") is not None) or sg == ""
    out = "/".join(sg if keep(sg) else mask(sg, 40) for sg in segs)
    if query:
        out += "?" + "&".join(f"{k}={mask(v, 20)}" for k, _, v in (q.partition("=") for q in query.split("&")))
    return out


def _mask_json(v, depth=0):
    """Behold struktur, nøgler, tal, bools og stier/URL'er. Maskér al anden tekst."""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: _mask_json(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [_mask_json(x, depth + 1) for x in v[:3]] + ([f"…+{len(v) - 3}"] if len(v) > 3 else [])
    if isinstance(v, str):
        if v.startswith(("/", "http")) and " " not in v:
            return mask_path(urlsplit(v).path or v)
        return mask(v, 30)
    return v


def _label(el) -> str:
    cls = ".".join(el.get("class", []))[:60]
    ident = f"#{el['id']}" if el.get("id") else ""
    return f"<{el.name}{ident}{'.' + cls if cls else ''}>"


def skeleton(root, max_lines: int = 140) -> list[str]:
    """Træ af hovedindholdet. Gentagne søskende med samme tag+klasse foldes til 'xN'."""
    out = []

    def walk(node, depth):
        if depth > 10 or len(out) >= max_lines:
            return
        kids = [c for c in node.children if getattr(c, "name", None) and c.name not in ("script", "style", "svg")]
        groups = []
        for c in kids:
            key = _label(c)
            if groups and groups[-1][0] == key:
                groups[-1][1].append(c)
            else:
                groups.append((key, [c]))
        for key, members in groups:
            c = members[0]
            own = " ".join(t.strip() for t in c.find_all(string=True, recursive=False) if t.strip())
            extra = f" x{len(members)}" if len(members) > 1 else ""
            href = f" href={mask_path(urlsplit(c['href']).path)}" if c.name == "a" and c.get("href") else ""
            out.append(f"{'  ' * depth}{key}{extra}{href}" + (f" '{mask(own, 40)}'" if own else ""))
            walk(max(members, key=lambda m: len(m.find_all(True))), depth + 1)

    walk(root, 0)
    return out


def url_values(v, path="") -> list[tuple[str, str]]:
    """Alle streng-værdier der ligner stier/URL-skabeloner, med deres nøglesti (maskeret)."""
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            out += url_values(x, f"{path}.{k}" if path else k)
    elif isinstance(v, list):
        for i, x in enumerate(v[:1]):
            out += url_values(x, f"{path}[0]")
    elif isinstance(v, str) and v.startswith("/") and " " not in v and len(v) < 200:
        out.append((path, mask_path(v)))
    return out


def list_shapes(v, path="") -> list[str]:
    """Nøglerne i første element af lister med objekter - strukturen af fx beskedlisten."""
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            out += list_shapes(x, f"{path}.{k}" if path else k)
    elif isinstance(v, list) and v and isinstance(v[0], dict):
        out.append(f"{path}[{len(v)}]: {sorted(v[0].keys())}")
        out += list_shapes(v[0], f"{path}[0]")
    return out


_SCRIPT_URL = re.compile(r"[\"'](/(?:parent|notifications|messages|news|calendar|api)[A-Za-z0-9_\-/{}.?=&]*)[\"']")


def script_urls(soup) -> list[str]:
    found = []
    for sc in soup.find_all("script"):
        for m in _SCRIPT_URL.finditer(sc.string or ""):
            u = mask_path(m.group(1))
            if u not in found:
                found.append(u)
    return found[:60]


def _nav_links(soup, host: str) -> list[tuple[str, str]]:
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if href.startswith("http") and urlsplit(href).netloc != host:
            continue
        path = urlsplit(href).path
        if path in seen:
            continue
        seen.add(path)
        in_nav = a.find_parent(class_=UI_CONTAINERS) is not None
        text = a.get_text(" ", strip=True)
        links.append((path, text[:40] if in_nav else mask(text, 40)))
    return links


def run(si) -> int:
    landing = si.ensure_child_url() or si.get("")
    host = urlsplit(landing.url).netloc
    log.info("map: child_url=%s, landing=%s", si.child_url, urlsplit(landing.url).path)
    time.sleep(PAUSE_S)
    index = si.get(f"{si.child_url}/Index")
    soup = BeautifulSoup(landing.text + index.text, "lxml")
    links = _nav_links(soup, host)
    log.info("map: %d links (navigationstekst i klar, øvrig tekst maskeret):", len(links))
    for path, text in links[:150]:
        log.info("  %-70s %s", mask_path(path), text)

    # Kun oversigtssider - aldrig enkeltbeskeder/-opslag (stier der ender på et id)
    candidates = sorted({p for p, _ in links if KEYWORDS.search(p) and OVERVIEW.match(p)}, key=len)[:MAX_PAGES]
    log.info("map: kandidat-sider (%d): %s", len(candidates), candidates)
    for path in candidates:
        time.sleep(PAUSE_S)
        try:
            r = si.get(path)
        except SkoleIntraError:
            raise  # loginværn/bot-beskyttelse: stop straks
        except Exception as e:
            log.info("map: %s FEJL %s", path, e)
            continue
        log.info("map: ===== %s -> HTTP %s, %s, %d tegn", path, r.status_code, urlsplit(r.url).path, len(r.text))
        settings = parser.client_settings(r.text)
        for k, v in settings.items():
            for kp, u in url_values(v):
                log.info("map: url  settings[%s].%s = %s", k, kp, u)
            for shape in list_shapes(v):
                log.info("map: list settings[%s].%s", k, shape)
        page = BeautifulSoup(r.text, "lxml")
        for u in script_urls(page):
            log.info("map: url  script: %s", u)
        main = page.find("main") or page.find(id="root") or page.body or page
        for tag in main.find_all(["nav", "header", "footer"]):
            tag.decompose()
        for line in skeleton(main):
            log.info("map:   %s", line)
    log.info("map: færdig - intet gemt på disk")
    return 0

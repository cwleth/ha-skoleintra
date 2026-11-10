"""Valgfri AI-kondensering via Claude. Kaldes kun når planen har ændret sig
(SHA-256-cache), og falder tilbage til den rå plan ved enhver fejl."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("skoleintra.ai")

SYSTEM = """Du omskriver en dansk skoles lektiebog til et kort, handlingsorienteret overblik for en forælder.

Returnér KUN gyldig JSON med præcis denne struktur:
{"overblik": "1-2 sætninger",
 "vigtigt": ["afleveringer, frister, prøver, ting der kræver handling"],
 "medbring": ["ting barnet skal have med, med dag i parentes"],
 "dage": [{"dag": "dagens label som i input", "punkter": [{"fag": "...", "opgave": "...", "frist": "kun hvis nævnt"}]}]}

Regler:
- Opfind ALDRIG datoer, frister, sidetal, opgavenumre eller materialer. Står det ikke i input, står det ikke i output.
- Er noget uklart, så gengiv lærerens formulering ordret i stedet for at fortolke.
- Udelad tomme felter og dage uden indhold. Udelad "frist" hvis ingen frist er nævnt.
- Skriv kort og på dansk."""


def condense(plan: dict, api_key: str, model: str, cache_file: Path) -> dict | None:
    raw = json.dumps({"days": plan["days"]}, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256(f"{model}\0{raw}".encode()).hexdigest()
    try:
        cache = json.loads(cache_file.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        log.debug("AI-cache hit")
        return cache[key]

    try:
        import anthropic

        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        result = json.loads(text)
        if not isinstance(result, dict) or "dage" not in result:
            raise ValueError("uventet struktur")
    except Exception as e:  # AI er pynt - må aldrig vælte kørslen
        log.warning("AI-kondensering fejlede (%s) - bruger rå plan", e)
        return None

    cache_file.write_text(json.dumps({key: result}, ensure_ascii=False), "utf-8")  # kun seneste
    log.info("AI-kondensering gennemført (%s)", model)
    return result


def to_markdown(c: dict, title: str) -> str:
    out = [f"## {title}"]
    if c.get("overblik"):
        out.append(c["overblik"])
    if c.get("vigtigt"):
        out.append("### Vigtigt\n" + "\n".join(f"- {x}" for x in c["vigtigt"]))
    if c.get("medbring"):
        out.append("### Medbring\n" + "\n".join(f"- {x}" for x in c["medbring"]))
    for d in c.get("dage", []):
        pts = d.get("punkter") or []
        if not pts:
            continue
        lines = []
        for p in pts:
            line = f"- **{p.get('fag', '')}**: {p.get('opgave', '')}"
            if p.get("frist"):
                line += f" *(frist: {p['frist']})*"
            lines.append(line)
        out.append(f"### {d.get('dag', '')}\n" + "\n".join(lines))
    return "\n\n".join(out)

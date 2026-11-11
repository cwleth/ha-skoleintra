"""msgprobe: afklarer om indlæsning af en beskedtråd markerer den som læst.

Logger KUN struktur og booleans: ingen emner, navne, tekster eller id'er.
Gemmer intet på disk ud over rate limit-tidsstemplet.

Standard (msgprobe_unread: false): tester på én allerede LÆST tråd.
Det verificerer request-formatet uden risiko.

msgprobe_unread: true: tester på én ULÆST tråd. Hvis tråden bliver
markeret som læst, sættes de berørte beskeder straks tilbage til ulæst
via /messages/UpdateMessagesReadState (isRead=false), og resultatet
verificeres med en ny hentning af listen.

Request-formater (fra fskintra/pgDialogue.py og foraeldreintra-cli):
  GET  {barn}/messages/conversations/loadmessagesforselectedconversation
       ?threadId=..&takeFromRootMessageId=<LatestMessageId>&takeToMessageId=0&searchRequest=
  GET  {barn}/messages/conversations/getmessageforthreadlessconversation?messageId=..
  POST {barn}/messages/UpdateMessagesReadState
       selectionState[MessageIds][]=<id>&isRead=true|false
"""

from __future__ import annotations

import json
import logging
import time

from bs4 import BeautifulSoup

from .client import SkoleIntra, SkoleIntraError

log = logging.getLogger("skoleintra.msgprobe")
ATTR = "data-clientlogic-settings-messageconversationswitharchivefunctionality"
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}


def _conversations(si: SkoleIntra) -> list[dict]:
    r = si.get(f"{si.child_url}/messages/conversations")
    soup = BeautifulSoup(r.text, "lxml")
    el = soup.find(attrs={ATTR: True})
    if el is None:
        raise SkoleIntraError("Beskedsiden har ikke den forventede JSON-attribut")
    convs = json.loads(el[ATTR]).get("Conversations") or []
    log.info("Beskedliste: %d tråde, %d ulæste", len(convs), sum(bool(c.get("IsUnread")) for c in convs))
    return convs


def _key(c: dict) -> str:
    return str(c.get("ThreadId") or c.get("LatestMessageId"))


def _state(convs: list[dict], key: str) -> tuple[bool | None, int | None]:
    for c in convs:
        if _key(c) == key:
            return bool(c.get("IsUnread")), c.get("UnreadMessagesCount")
    return None, None


def _ajax_get(si: SkoleIntra, path: str, params: dict) -> list[dict]:
    url = f"{si.base}/{si.child_url}/{path}"
    params = dict(params, _=str(int(time.time() * 1000)))
    referer = f"{si.base}/{si.child_url}/messages/conversations"
    r = si._request("GET", url, params=params, headers=dict(AJAX, Referer=referer))
    log.info("Hentning: HTTP %s, content-type %s, %d bytes",
             r.status_code, r.headers.get("content-type", "?").split(";")[0], len(r.content))
    if r.status_code != 200:
        raise SkoleIntraError(f"Tråd-hentning gav HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise SkoleIntraError("Tråd-hentning returnerede ikke JSON (formatet er forkert)")
    msgs = data if isinstance(data, list) else [data]
    keys = sorted(msgs[0].keys()) if msgs and isinstance(msgs[0], dict) else []
    log.info("Tråd: %d beskeder; feltnavne: %s", len(msgs), ", ".join(keys))
    log.info("Tråd: %d beskeder med ShowUnreadIndication=true",
             sum(bool(m.get("ShowUnreadIndication")) for m in msgs if isinstance(m, dict)))
    return msgs


def _set_unread(si: SkoleIntra, ids: list[str]) -> int:
    url = f"{si.base}/{si.child_url}/messages/UpdateMessagesReadState"
    data = [("selectionState[MessageIds][]", i) for i in ids] + [("isRead", "false")]
    referer = f"{si.base}/{si.child_url}/messages/conversations"
    r = si._request("POST", url, data=data, headers=dict(AJAX, Referer=referer))
    log.info("Gendan ulæst: HTTP %s for %d beskeder", r.status_code, len(ids))
    return r.status_code


def run(si: SkoleIntra, unread: bool) -> int:
    si.ensure_child_url()
    convs = _conversations(si)
    target = next((c for c in convs if bool(c.get("IsUnread")) == unread and c.get("LatestMessageId")), None)
    if target is None:
        log.warning("Ingen %s tråd fundet - intet testet", "ulæst" if unread else "læst")
        return 0
    key = _key(target)
    threaded = bool(target.get("ThreadId"))
    before = _state(convs, key)
    log.info("Testtråd: %s, trådet=%s, før: IsUnread=%s UnreadMessagesCount=%s",
             "ULÆST" if unread else "læst", threaded, *before)

    if threaded:
        msgs = _ajax_get(si, "messages/conversations/loadmessagesforselectedconversation", {
            "threadId": target["ThreadId"],
            "takeFromRootMessageId": str(target["LatestMessageId"]),
            "takeToMessageId": "0",
            "searchRequest": "",
        })
    else:
        msgs = _ajax_get(si, "messages/conversations/getmessageforthreadlessconversation",
                         {"messageId": str(target["LatestMessageId"])})
    flagged = [str(m["Id"]) for m in msgs if isinstance(m, dict) and m.get("ShowUnreadIndication") and m.get("Id")]

    time.sleep(5)
    after = _state(_conversations(si), key)
    log.info("Efter hentning: IsUnread=%s UnreadMessagesCount=%s", *after)

    if not unread:
        log.info("RESULTAT: request-formatet virker (læst tråd, ingen status at ændre).")
        return 0
    if after[0]:
        log.info("RESULTAT: Hentning markerer IKKE som læst. Tråden er stadig ulæst.")
        return 0

    log.warning("RESULTAT: Hentning MARKEREDE tråden som læst. Gendanner ulæst ...")
    ids = flagged or [str(target["LatestMessageId"])]
    _set_unread(si, ids)
    time.sleep(5)
    restored = _state(_conversations(si), key)
    log.info("Efter gendannelse: IsUnread=%s UnreadMessagesCount=%s (før: %s/%s)", *restored, *before)
    if restored[0]:
        log.info("RESULTAT: ulæst-status er gendannet.")
        return 0
    log.error("RESULTAT: gendannelse mislykkedes - markér tråden som ulæst manuelt i ForældreIntra.")
    return 1

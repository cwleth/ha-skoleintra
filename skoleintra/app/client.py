"""HTTP-klient til ForældreIntra.

Login-flowet følger CavaleriDK/skoleintra (MIT): ASP.NET-form med UserName/
Password, derefter et auto-post SAML-form (id="samlform") som skal sendes
videre manuelt, fordi browseren normalt gør det med JavaScript.

Bevidste afvigelser fra referencen:
- Højst ÉN login-POST pr. kørsel (referencen prøver 3 gange = spærregrænsen).
- Loginværn på disk: afviste oplysninger prøves aldrig igen, og efter
  MAX_REJECTS afvisninger siden sidste succes stopper alle loginforsøg.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("skoleintra.client")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_REJECTS = 2  # SkoleIntra spærrer ved 3 - vi holder ét forsøg i reserve


class SkoleIntraError(Exception):
    status = "fejl"


class LoginRejected(SkoleIntraError):
    status = "login_afvist"


class LoginBlocked(SkoleIntraError):
    status = "login_blokeret"


class AutomationBlocked(SkoleIntraError):
    status = "bot_blokeret"


class UnsupportedLogin(SkoleIntraError):
    status = "login_ikke_understoettet"


class LoginGuard:
    """Husker afviste logins på tværs af kørsler og genstarter."""

    def __init__(self, path: Path, username: str, password: str):
        self.path = path
        self.cred = hashlib.sha256(f"{username}\0{password}".encode()).hexdigest()
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {"rejects": 0, "rejected_creds": [], "reset_consumed": False}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), "utf-8")

    def check(self) -> None:
        if self.cred in self.state["rejected_creds"]:
            raise LoginBlocked(
                "Disse loginoplysninger er afvist før og prøves ikke igen. "
                "Ret brugernavn/kode i add-on'ens konfiguration."
            )
        if self.state["rejects"] >= MAX_REJECTS:
            raise LoginBlocked(
                f"{self.state['rejects']} afviste logins siden sidste succes - "
                "stopper for at undgå kontospærring. Log ind manuelt i browseren "
                "én gang, og slå derefter reset_login_guard til."
            )

    def rejected(self) -> None:
        self.state["rejects"] += 1
        self.state["rejected_creds"].append(self.cred)
        self._save()

    def success(self) -> None:
        self.state.update(rejects=0, rejected_creds=[])
        self._save()

    def handle_reset_option(self, requested: bool) -> bool:
        """Nulstil én gang pr. gang indstillingen slås til. Returnerer True ved reset."""
        if requested and not self.state.get("reset_consumed"):
            self.state.update(rejects=0, rejected_creds=[], reset_consumed=True)
            self._save()
            return True
        if not requested and self.state.get("reset_consumed"):
            self.state["reset_consumed"] = False
            self._save()
        return False


class SkoleIntra:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        state_dir: Path,
        child_url: str | None = None,
    ):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.state_dir = state_dir
        self.cookie_file = state_dir / "cookies.pkl"
        self.meta_file = state_dir / "session.json"
        self.guard = LoginGuard(state_dir / "login_guard.json", username, password)
        self.login_attempted = False

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "da,en;q=0.7"})
        self._load_cookies()
        self.child_url = (child_url or "").strip("/") or self._load_meta().get("child_url")

    # ---------- persistens ----------
    def _load_cookies(self) -> None:
        try:
            self.s.cookies.update(pickle.loads(self.cookie_file.read_bytes()))
            log.debug("Genbruger gemt cookie-session")
        except (OSError, pickle.UnpicklingError, EOFError):
            log.debug("Ingen gemt cookie-session")

    def _load_meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file.write_bytes(pickle.dumps(self.s.cookies))
        self.meta_file.write_text(json.dumps({"child_url": self.child_url}), "utf-8")

    # ---------- HTTP-hjælpere ----------
    def _request(self, method: str, url: str, **kw) -> requests.Response:
        r = self.s.request(method, url, timeout=30, **kw)
        if self._is_automation_block(r):
            raise AutomationBlocked(
                "SkoleIntra blokerede forespørgslen som automatisering (HTTP 500). "
                "Vent til næste planlagte kørsel."
            )
        return r

    @staticmethod
    def _is_automation_block(r: requests.Response) -> bool:
        if r.status_code != 500:
            return False
        soup = BeautifulSoup(r.text, "lxml")
        return len(soup.find_all("h1")) == 1 and len(soup.find_all("h2")) == 1

    def _pass_saml(self, r: requests.Response) -> requests.Response:
        for _ in range(3):  # normalt ét hop; loft mod løkker
            form = BeautifulSoup(r.text, "lxml").find("form", id="samlform")
            if form is None:
                return r
            data = {
                i.get("name"): i.get("value", "")
                for i in form.find_all("input")
                if i.get("name")
            }
            action = urljoin(r.url, form.get("action") or "")
            log.debug("Videresender SAML-form til %s", action)
            r = self._request("POST", action, data=data)
        return r

    @staticmethod
    def has_login_form(html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        return bool(soup.find(id="UserName") or soup.find("input", attrs={"name": "UserName"}))

    # ---------- login ----------
    def login(self) -> requests.Response:
        if self.login_attempted:
            raise SkoleIntraError("Login er allerede forsøgt i denne kørsel - prøver ikke igen.")
        self.guard.check()

        r = self._pass_saml(self._request("GET", self.base))
        soup = BeautifulSoup(r.text, "lxml")
        user_input = soup.find("input", attrs={"name": "UserName"})
        form = user_input.find_parent("form") if user_input else None
        if form is None or not form.find("input", attrs={"name": "Password"}):
            raise UnsupportedLogin(
                "Login-siden har ikke felterne UserName/Password. Skolen bruger "
                "sandsynligvis MitID/UniLogin, som ikke kan automatiseres."
            )

        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            if name == "UserName":
                data[name] = self.username
            elif name == "Password":
                data[name] = self.password
            else:
                data[name] = inp.get("value", "")

        action = urljoin(r.url, form.get("action") or "")
        self.login_attempted = True  # sættes FØR POST: aldrig to forsøg
        log.info("Logger ind på %s (ét forsøg)", urlsplit(self.base).netloc)
        r = self._pass_saml(self._request("POST", action, data=data))

        if self.has_login_form(r.text):
            self.guard.rejected()
            raise LoginRejected(
                "ForældreIntra afviste brugernavn/kode. Nye forsøg med samme "
                "oplysninger er blokeret for at undgå kontospærring."
            )

        self.guard.success()
        self._set_child_url(r.url)
        self.save()
        log.info("Logget ind. Barnets URL: %s", self.child_url)
        return r

    def _set_child_url(self, url: str) -> None:
        path = urlsplit(url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 3 and parts[0].lower() == "parent":
            self.child_url = "/".join(parts[:3])
        elif not self.child_url:
            log.warning("Kunne ikke udlede barnets URL fra %s - sæt child_url manuelt", url)

    # ---------- navigation ----------
    def get(self, path: str) -> requests.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        r = self._pass_saml(self._request("GET", url))
        if self.has_login_form(r.text):
            self.login()
            r = self._pass_saml(self._request("GET", url))
            if self.has_login_form(r.text):
                raise SkoleIntraError("Stadig ikke logget ind efter vellykket login.")
        self.save()
        return r

    def ensure_child_url(self) -> requests.Response | None:
        if self.child_url:
            return None
        r = self.get("")
        if not self.child_url:
            self._set_child_url(r.url)
        if not self.child_url:
            raise SkoleIntraError("Barnets URL kunne ikke findes - sæt child_url i konfigurationen.")
        self.save()
        return r

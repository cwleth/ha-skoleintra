# SkoleIntra add-on (lokal)

Henter ugeplanen fra ForældreIntra og lægger den i Home Assistant via MQTT discovery.

## Installation
1. Kopiér mappen `skoleintra` ind i Samba-sharet `addons`.
2. Indstillinger → Tilføjelsesprogrammer → Butik → ⋮ → Søg efter opdateringer.
   "SkoleIntra" ligger under Lokale tilføjelsesprogrammer → Installér.
3. Konfiguration: udfyld brugernavn og kode. Lad `mode: discover` stå → Start.
4. Når parseren er bekræftet: `mode: once` → Start (opretter sensorerne).
5. Drift: `mode: schedule`, slå "Start ved opstart" og "Watchdog" til.

## Modes
| mode | gør |
|---|---|
| discover | logger ind, dumper sider til `/share/skoleintra_dump`, stopper |
| dry-run | henter og parser, skriver i loggen, rører ikke HA |
| once | én kørsel: sensorer + evt. notifikation, stopper |
| schedule | kører på `run_at`-tidspunkterne, bliver kørende |

## Entiteter (enheden "SkoleIntra")
- `sensor.skoleintra_ugeplan` – state "Uge 46: 12 punkter", hele planen i attributter (`markdown`, `days`, `general`, `changes`, `condensed`)
- `sensor.skoleintra_status` – ok / login_afvist / login_blokeret / bot_blokeret / login_ikke_understoettet / fejl
- `sensor.skoleintra_sidst_hentet` – tidsstempel for seneste vellykkede hentning

Sensorerne er retained MQTT og overlever genstart af HA.

Dashboard-kort (Markdown-kort):
```yaml
type: markdown
content: "{{ state_attr('sensor.skoleintra_ugeplan', 'markdown') }}"
```

## Loginværn
SkoleIntra spærrer kontoen efter 3 fejlede logins. Add-on'en:
- poster højst ét login pr. kørsel og genbruger cookie-sessionen,
- prøver aldrig afviste oplysninger igen,
- stopper helt efter 2 afvisninger siden sidste succes.

Ved blokering: log ind manuelt i browseren én gang, ret evt. koden, slå
`reset_login_guard` til, start, og slå den fra igen. Nulstillingen virker kun én
gang pr. gang den slås til.

## Ikke verificeret endnu
- Ugeplan-URL'en med ISO-uge (`.../item/class/46-2026`) mod det rigtige site.
- Dagbogen: kun dump, ingen parser før strukturen kendes.
- Byg på HAOS og MQTT mod den rigtige Mosquitto.

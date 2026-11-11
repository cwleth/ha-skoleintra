# SkoleIntra add-on

Henter lektiebogen fra ForældreIntra (Lektier → Lektiebog) og lægger den i Home Assistant via MQTT discovery. Klassens ugeplan bruges ikke: Skolen giver forældre `notauthorized` på den.

## Installation
1. Tilføj `https://github.com/cwleth/ha-skoleintra` under Butik → ⋮ → Lagre.
2. Installér "SkoleIntra".
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
- `sensor.skoleintra_lektier` – state "4 punkter de næste 3 skoledage"; attributter `days`, `markdown`, `changes`, `condensed`, `diary_id` (14 dage frem)
- `sensor.skoleintra_naeste_skoledag` – næste dag med lektier (efter kl. 14 springes i dag over); attributter `date`, `lessons`, `markdown`
- `sensor.skoleintra_status` – ok / login_afvist / login_blokeret / bot_blokeret / login_ikke_understoettet / fejl
- `sensor.skoleintra_sidst_hentet` – tidsstempel for seneste vellykkede hentning

## To-do-lister (én pr. skoledag)
- `todo.lektier_dag_1` … `todo.lektier_dag_5` – rullende: dag 1 er første dag fra i dag med lektier, dag 2 den næste osv. Hver morgen rykker posterne op, og afkrydsning følger med.
- `todo.lektier_backlog` – lektier der ikke er krydset af, når dagen er passeret.
- Kun én kort linje pr. lektie (≤ 44 tegn), ingen beskrivelse. Fag "Info" springes over.
- Sensoren `sensor.skoleintra_lektier` har attributten `slots` (datoen for dag 1-5) til dashboard-overskrifter.
- Slå fra med `todo_sync: false`.

Sensorerne er retained MQTT og overlever genstart af HA.

Dashboard-kort (Markdown-kort):
```yaml
type: markdown
content: "{{ state_attr('sensor.skoleintra_lektier', 'markdown') }}"
```

## Loginværn
SkoleIntra spærrer kontoen efter 3 fejlede logins. Add-on'en:
- poster højst ét login pr. kørsel og genbruger cookie-sessionen,
- prøver aldrig afviste oplysninger igen,
- stopper helt efter 2 afvisninger siden sidste succes.

Ved blokering: log ind manuelt i browseren én gang, ret evt. koden, slå
`reset_login_guard` til, start, og slå den fra igen. Nulstillingen virker kun én
gang pr. gang den slås til.

## Lektiebogs-ID
Findes automatisk på siden Lektier og gemmes. Hvis ID'et holder op med at virke
(fx ved nyt skoleår), findes det nye automatisk. `diary_id` kan låse det.

## Stier (verificeret 11-11-2026)
SkoleIntra sætter `item` direkte efter barnets navn uden skråstreg:
`/parent/<id>/<navn>item/weeklyplansandhomework/diary/notes/<dagbogs-id>`

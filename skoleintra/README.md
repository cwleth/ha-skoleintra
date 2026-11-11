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
- `todo.lektier_dag_1` … `todo.lektier_dag_7` – rullende: dag 1 er første dag fra i dag med indhold, dag 2 den næste osv. Når dagene rykker, flyttes posterne med deres afkrydsning.
- Afkrydsede lektier bliver stående (overstreget), indtil dagen er gået. Herefter fjernes de, og åbne lektier flyttes til `todo.lektier_backlog`.
- Backlog: afkrydsede poster bliver stående resten af dagen og ryddes næste morgen. Åbne poster får første linje "N dage over tid · skulle laves <dag>" i beskrivelsen (opdateres ved hver kørsel).
- **Info vs. lektier:** faget "Info" og varsler ("lektie … til næste uge" uden "i dag") bliver ikke til to-do-poster. De vises som noter på dagen via `sensor.skoleintra_dag_N`.
- Lærerens tekst ligger kondenseret i postens beskrivelse (opgavelinjen først, uden links og støj) - vises med lille skrift under linjen. Den fulde tekst ligger i `sensor.skoleintra_dag_N` (attribut `full`).
- Én kort linje pr. lektie (≤ 38 tegn; lange tekster med sidetal bliver til fx "Skakbogen s. 14+15").
- Slå fra med `todo_sync: false`.

## Fag kun på bestemte dage
`subject_days: "Skak:man"` sorterer indlæg for faget fra på andre ugedage (fx når lektiebogen indeholder både mandags- og onsdagsholdet). Flere: `"Skak:man, Svømning:tir+tor"`. Det skjulte står i `sensor.skoleintra_lektier` (attributter `hidden`, `subject_days`), så dashboardet kan vise, hvad filteret fjerner.

## Dag-sensorer (til dashboardet)
- `sensor.skoleintra_dag_1` … `_7`: state = antal lektier den dag (afkrydsede tæller med), `-1` = ingen dag i pladsen. Attributter: `date`, `notes`, `full` (lærerens fulde tekst pr. fag, til fold-ud).
- `sensor.skoleintra_backlog`: antal poster i backlog inkl. afkrydsede. Attribut `items` (summary, description, done) til læse-pop-up.

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

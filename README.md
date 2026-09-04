# ai-orchestrator

Lokální AI orchestrátor pro Windows 11. Řídí AI agenty (Claude Code, Gemini a
OpenAI Codex), kteří pracují na tvých projektech - spustí agenta na
zadaný úkol, spustí testy, a pokud vše projde a ty to povolíš, vytvoří Git
commit. Nic se neděje bez tvého vědomí a nic se nikdy neposílá na internet
mimo volání samotného AI modelu.

Tento návod nepředpokládá, že umíš programovat nebo pracovat s Gitem -
všechny příkazy níže stačí zkopírovat a spustit.

## 1. Jednorázová příprava

Otevři terminál (PowerShell) v tomto adresáři (`D:\orchestrator\ai-orchestrator`)
a spusť:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Tím se vytvoří izolované prostředí `.venv` jen pro tento projekt - nic to
neovlivní jinde v systému.

## 2. Přihlášení Claude Code CLI

Orchestrátor spouští Claude Code jako samostatný program (CLI), a ten
potřebuje být přihlášený nezávisle na desktopové aplikaci Claude. Over to
takto:

```bash
.venv\Scripts\python orchestrator.py doctor --live
```

Pokud `doctor` napíše něco jako "Not logged in", otevři terminál a spusť
jednou ručně (bez `-p`, tedy interaktivně):

```bash
claude
```

nebo, pokud `claude` není v PATH, cestu k `claude.exe`, kterou ti vypíše
`doctor` (řádek "Claude Code CLI"). Přihlas se podle pokynů na obrazovce a
pak zkus `doctor --live` znovu. Toto je jediný krok, který musíš provést
ručně - orchestrátor za tebe nikdy nezadává hesla ani se sám nepřihlašuje.

### Ověření reálného Codex CLI kontraktu (volitelný živý test)

Stejně jako u Claude Code (viz výše), `doctor --live` teď ověří i reálný
Codex CLI kontrakt - žádný zvláštní příkaz navíc není potřeba:

```bash
.venv\Scripts\python orchestrator.py doctor --live
```

Pokud je `codex` nalezen (řádek "Codex CLI" v základním, ne-live výpisu
`doctor`), `doctor --live` navíc spustí řádek "Codex CLI (živý test)": pošle
Codexu jeden read-only dotaz s vynuceným `--sandbox read-only` (bez ohledu na
nakonfigurovaný `codex.sandbox_mode`) a požadovaným `--output-schema`
kontraktem, a ověří, že (a) odpověď odpovídá schématu a (b) se v adresáři,
proti kterému `doctor --live` běží, nic nezměnilo. Vyžaduje lokálně
nainstalovaný a přihlášený Codex CLI - spuštění stojí malé množství skutečné
Codex kvóty, proto to `doctor` bez `--live` nikdy nedělá sám od sebe.

Stejnou kontrolu lze spustit i přímo jako pytest test (např. v CI, kde
`orchestrator.py doctor` není zvykem volat):

```bash
.venv\Scripts\python -m pytest tests/test_codex_agent.py::test_live_smoke_reads_project_state_without_changes -q
```

Před spuštěním nastav `AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST=1` (jinak se test
přeskočí, viz `AGENTS.md` - testy nesmí v základní sadě volat placené API).
Autonomní vývojová iterace ani `doctor --live` sama o sobě nikdy nespouští -
nemá k tomu oprávnění ani přístup k živému `codex` CLI - tento krok musí
provést člověk (nebo CI) s přístupem k reálnému, přihlášenému Codex CLI.

### Gemini CLI jako free-tier provider

Gemini používá v automatickém pořadí výhradně explicitní model
`gemini-2.5-flash`. Headless adapter vyžaduje JSON výstup, režim
`auto_edit` a `--skip-trust` pouze pro zvolený pracovní adresář; nikdy
nepoužívá `--yolo` ani tichý přechod na jiný model. Ověření instalace bez
spuštění modelu:

```bash
gemini --version
```

Samotný `gemini --version` nepotvrzuje přihlášení ani dostupnost free-tieru.
Při živém ověření 31. 8. 2026 nainstalovaný CLI vrátil
`UNSUPPORTED_CLIENT` / `IneligibleTierError` pro Gemini Code Assist for
individuals. To je stav účtu/CLI mimo orchestrátor; adapter jej vrací jako
selhání a failover pokračuje dalším providerem, bez tvrzení, že Gemini úlohu
provedl. Po migraci CLI/účtu na podporovaný přístup lze stejný provider znovu
ověřit bez změny konfigurace modelu.

## 3. Kontrola prostředí (doctor)

```bash
.venv\Scripts\python orchestrator.py doctor
```

Zkontroluje Python, Git, Claude Code CLI, pracovní adresáře a konfiguraci.
Přidej `--live`, pokud chceš navíc ověřit, že přihlášení a volání Claude
Code opravdu funguje (stojí to pár centů, proto to není v základním testu).

## 4. Nastavení projektů

Otevři `config/config.yaml` (vytvoří se automaticky při prvním spuštění ze
vzoru `config/config.example.yaml`) a do sekce `projects` přidej své
projekty, např.:

```yaml
projects:
  ai-orchestrator:
    path: "D:/orchestrator/ai-orchestrator"
  station-agent:
    path: "D:/orchestrator/station-agent"   # nemusí ještě existovat
    test_command: "pytest"
```

Cesta projektu nemusí předem existovat - pokud adresář chybí, orchestrátor
ho při prvním úkolu na daném projektu sám založí, takže agent může založit
úplně nový projekt od nuly (např. `station-agent` vedle `ai-orchestrator`).
Jediné omezení: cesta musí ležet uvnitř pracovního prostoru nastaveného v
`workspace_root` (výchozí je nadřazený adresář tohoto repozitáře, tedy
`D:\orchestrator`) - mimo něj orchestrátor a Claude Code nikdy nesmí
pracovat, a to i kdyby ses překlepl v `config.yaml`.

`auto_commit: false` je výchozí nastavení - orchestrátor tedy sám necommituje,
dokud to buď v `config.yaml` (sekce `git`) ručně nezapneš pro všechny budoucí
běhy, nebo dokud to výslovně nepovolíš jen pro jeden konkrétní běh přepínačem
`--commit` (viz níže) - obě cesty stále platí jen společně s pravidlem "nikdy
necommitovat, když testy selžou".

## 5. Spuštění úkolu

```bash
.venv\Scripts\python orchestrator.py run "Přidej do README.md sekci Instalace" --project muj-projekt
```

Orchestrátor spustí Claude Code na daném projektu, počká na výsledek, podle
konfigurace spustí testy, a vypíše, co se stalo (výsledek, výstup testů,
zda vznikl commit). Detailní log najdeš v `logs/tasks/<id>.log` a strojově
čitelný výsledek v `outbox/<id>.json`.

## 6. Autonomní vývojový režim

Kromě jednorázového `run` umí orchestrátor i autonomní režim: zadáš projekt
a "Definition of Done" (co musí platit, aby byl úkol hotový) a orchestrátor
sám opakuje cyklus **implementace -> testy -> vyhodnocení -> oprava**, dokud
Definition of Done není splněná, nebo dokud nedosáhne bezpečného maximálního
počtu iterací (výchozí 10, natvrdo omezeno na 50 bez ohledu na to, co
zadáš).

```bash
.venv\Scripts\python orchestrator.py autonomous --project station-agent --goal "Zaloz projekt station-agent a napis health-check endpoint" --spec dod-station-agent.md --max-iterations 10
```

`dod-station-agent.md` je obyčejný textový soubor s Definition of Done,
jeden bod na řádek, klidně jako checklist:

```markdown
- [ ] Existuje FastAPI endpoint /health, ktery vraci {"status": "ok"}
- [ ] Endpoint ma test, ktery overi 200 a spravne telo odpovedi
- [ ] README popisuje, jak endpoint spustit a otestovat
```

Produkční nebo integrační bod lze označit jako vyžadující živý důkaz. Deklarace
je Markdown komentář na konci bodu; AI Project Manager po provedení bezpečné
read-only kontroly přidá `LIVE-RESULT` (index je nulový index bodu v DoD):

```markdown
- [ ] Trello obsahuje projektový label <!-- LIVE-EVIDENCE: {"command":"načti labely karty","expect":"project_key"} -->
<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"label project_key nalezen"} -->
```

Orchestrátor příkaz z textu specifikace z bezpečnostních důvodů nespouští.
Výsledek vyhodnotí sám: musí mít `exit_code` 0 a `output` musí obsahovat
deklarované `expect`. Bez důkazu nebo při neshodě zůstane bod nesplněný, i
když jej agent označí hotový a všechny lokální testy projdou. Deklarace i
výsledek se ukládají do checkpointu, logu a outboxu pro zápis zpět do Trella.

Bez `--spec` stačí i jen `--goal` - použije se jako jediný bod Definition of
Done. Co se děje v každé iteraci:

1. Agent dostane cíl, aktuální (nesplněné) body Definition of Done, aktuální
   stav projektu (`git status`), výsledek testů z minulé iterace a svou
   vlastní poznámku z minulé iterace.
2. Agent upraví/doplní kód.
3. Pokud je nastavený testovací příkaz, orchestrátor spustí testy.
4. Agent sám vyhodnotí, které body Definition of Done jsou už splněné.
5. Každá iterace se zaloguje do `logs/autonomous/<id>.log` (a do
   `logs/orchestrator.log`).

Běh skončí jedním z těchto stavů:

- **completed** - všechny body Definition of Done splněné A testy prošly (nebo
  žádné testy nejsou nastavené). Pokud je navíc zapnutý `git.auto_commit` v
  `config.yaml`, nebo byl pro tento konkrétní běh výslovně předán `--commit`,
  vytvoří se Git commit. Pokud testy neprošly, commit se **nikdy** nevytvoří -
  ani s `--commit`.
- **waiting_for_provider** - všichni nakonfigurovaní provideři (viz
  `provider_order`, výchozí gemini → antigravity → claude-code → codex)
  jsou LIMITED
  nebo lokálně nedostupní. Běh se **neukončí jako chyba** - uloží se do fronty
  jako čekající task s `retry_after_seconds` a Definition of Done checkpointem
  (viz `data/autonomous_checkpoints/`), pošle se Slack notifikace s stavem
  KAŽDÉHO providera a nejbližším známým resetem, a výstup i
  `outbox/autonomous-<run_id>.json` řeknou přes `auto_resume_active`, jestli
  tento proces sám čekání dokončí (jen `orchestrator.py api`, viz kapitola 8),
  nebo je nutné po `retry_after_seconds` spustit **stejný příkaz znovu**
  (běžný případ pro `orchestrator.py autonomous`, které je jednorázový
  proces) - naváže z checkpointu, ne od bodu 0.
- **blocked** - stejný stav (stejné nesplněné body + stejný výsledek testů)
  se opakuje 3x po sobě bez posunu - orchestrátor to nezkouší dál dokola.
- **max_iterations** - vyčerpán limit iterací, Definition of Done pořád není
  splněná celá.
- **budget_exceeded** - aktivní provider překročil svůj nakonfigurovaný
  `max_budget_usd` (viz `claude_code`/`antigravity`/`codex` v
  `config.yaml`) pro TENTO běh a žádný další nakonfigurovaný provider
  nebyl k dispozici k failoveru. Provider-specific hard cap na útratu za
  jeden běh, nezávislý na `max_iterations`/`ABSOLUTE_MAX_ITERATIONS` (hard
  cap na počet iterací) - viz ARCHITECTURE.md "Per-job finanční limit".
- **error** - samotné volání agenta selhalo (např. timeout) - loop se hned
  zastaví.

Výsledek uvidíš přímo ve výstupu příkazu (které body jsou splněné/nesplněné,
jestli vznikl commit), detailní log v `logs/autonomous/<id>.log` a strojově
čitelný výsledek v `outbox/autonomous-<id>.json`.

Platí úplně stejná bezpečnostní pravidla jako pro `run` (viz níže) -
`workspace_root`, žádné `bypassPermissions`, žádný force push, žádné mazání
historie, žádný commit při selhaných testech.

## 7. Zjištění stavu úkolů

```bash
.venv\Scripts\python orchestrator.py status
.venv\Scripts\python orchestrator.py status <id-ukolu>
```

## 8. Lokální API (zatím jen pro tvůj počítač)

```bash
.venv\Scripts\python orchestrator.py api
```

Spustí HTTP API na `http://127.0.0.1:8765` (jen na tomto počítači, nikam
ven). To je příprava na budoucí propojení s jinými nástroji, např. mostem
z ChatGPT - zatím to nikam nepřipojujeme.

## 9. Adresáře inbox/outbox

`inbox/` a `outbox/` slouží pro automatické předávání úkolů/výsledků mezi
orchestrátorem a jiným nástrojem, viz README v každém z nich. Běžné úkoly
(`run`) lze do fronty dostat i ručně přes `inbox/*.json` a
`python orchestrator.py import-inbox`.

Autonomní běhy (`autonomous`) tudy neprochází - AI Project Manager je spouští
přímo přes CLI (`orchestrator.py autonomous --project <p> --spec <soubor>
--run-id <id-trello-karty>`) a výsledek čte zpět z
`outbox/autonomous-<run-id>.json`. Přesný, stabilní tvar tohoto JSON
kontraktu (co znamená `done`, `stop_reason`, `limit_hit`,
`provider_sequence`, `checkpoint`, ...) je popsaný v `outbox/README.md`.

## Bezpečnostní pravidla (proč se to takhle chová)

- Orchestrátor **nikdy** nepoužije `--dangerously-skip-permissions` ani
  jinou obdobu obcházení kontroly oprávnění - je to natvrdo zakázané i v
  konfiguraci (viz `AGENTS.md`).
- Agent smí pracovat jen uvnitř pracovního prostoru (`workspace_root`,
  výchozí `D:\orchestrator`) - jakýkoliv projekt mimo něj (v `config.yaml`
  i v `--project`) orchestrátor natvrdo odmítne.
- **Nikdy nesmaže Git historii** ani nepoužije force push. Push na internet
  v této fázi vůbec neexistuje.
- Commit vznikne jen tehdy, když to explicitně povolíš - buď natrvalo v
  konfiguraci (`git.auto_commit: true`), nebo jen pro jeden konkrétní běh
  přepínačem `--commit` - A zároveň testy prošly (nebo pro daný
  projekt žádné testy nejsou nastavené). Bez jednoho z těch dvou výslovných
  povolení orchestrátor necommituje nikdy, i kdyby úkol i testy dopadly
  bezvadně.
- API poslouchá jen na `127.0.0.1` - z internetu se k němu nedostaneš.

Další podrobnosti architektury jsou v [ARCHITECTURE.md](ARCHITECTURE.md),
pravidla pro AI agenty pracující v tomto repozitáři jsou v
[AGENTS.md](AGENTS.md).

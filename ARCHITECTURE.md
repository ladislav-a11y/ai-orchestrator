# Architektura

## Cílový pracovní tok

```
uživatel
    |
orchestrátor
    |
implementační agent   (ClaudeCodeAgent; později i CodexAgent)
    |
testy                 (volitelný test_command daného projektu)
    |
review agent          (zatím neaktivní - viz "Review agent" níže)
    |
[oprava, pokud testy selžou; max. `testing.max_fix_attempts` pokusů]
    |
testy
    |
git commit             (jen pokud auto_commit=true A testy prošly)
```

Toto je implementováno v `orchestrator/runner.py` funkcí `run_task`.

## Moduly

- `orchestrator/models.py` - `Task` (id, created_at, project, prompt, status,
  result, agent, error, + auditní pole jako attempts, test_output, commit_hash).
- `orchestrator/config.py` - načtení `config/config.yaml`, validace
  (natvrdo odmítne `permission_mode: bypassPermissions`, jiný `api.host`
  než localhost, a jakýkoliv projekt mimo `workspace_root`).
- `orchestrator/queue.py` - fronta úkolů v SQLite (`data/tasks.db`). SQLite
  proto, aby CLI i API mohly bezpečně číst stav, zatímco worker vlákno píše.
- `orchestrator/agents/` - abstraktní rozhraní agenta (`base.py`) a
  konkrétní implementace (`claude_code.py`). `registry.py` mapuje jméno
  agenta na třídu - takhle se přidá Codex bez zásahu do zbytku orchestrátoru.
- `orchestrator/git_utils.py` - tenký, záměrně konzervativní wrapper nad
  `git` (status, diff, commit). Žádné mazání historie, žádný force push,
  žádný push vůbec.
- `orchestrator/runner.py` - samotný pipeline výše.
- `orchestrator/service.py` - spojuje config + frontu + agenta + runner;
  sdílí ho CLI i API. Úkoly zpracovává jedno worker vlákno (žádné dva
  úkoly neběží na stejném projektu současně).
- `orchestrator/api.py` - lokální HTTP API (FastAPI/uvicorn), jen na
  `127.0.0.1`.
- `orchestrator/doctor.py` - kontrola prostředí.
- `orchestrator/cli.py` + `orchestrator.py` - příkazová řádka.

## Proč SQLite, ne jen JSON soubory v adresáři

Fronta potřebuje bezpečné souběžné čtení (CLI `status` i API `GET /tasks`
zatímco worker vlákno právě zapisuje průběh úkolu). SQLite je jeden lokální
soubor, nic se neinstaluje, a řeší zamykání za nás. `inbox/` a `outbox/`
zůstávají obyčejné adresáře se soubory, protože ty jsou určené pro výměnu
s vnějším světem (budoucí most), ne jako zdroj pravdy o stavu úkolu.

## Volání Claude Code CLI

`ClaudeCodeAgent` spouští `claude -p "<zadání>" --output-format json
--permission-mode <mode>` (`cwd` = adresář projektu). Nikdy nepřidá
`--dangerously-skip-permissions` ani `--allow-dangerously-skip-permissions`
- to je vynucené na dvou nezávislých místech (`config.py` při načtení
konfigurace a `ClaudeCodeAgent.__init__`/`_build_command`), aby stačilo
selhání jednoho z nich a druhé to stejně odchytí.

Důsledek: v neinteraktivním běhu nemá kdo odklikávat žádosti o oprávnění.
Proto výchozí `permission_mode: acceptEdits` (automaticky schvaluje úpravy
souborů, ale ne cokoliv riskantnějšího) a proto je důležité mít v cílovém
projektu (`.claude/settings.json`) předem povolené nástroje, které agent
bude opravdu potřebovat. Pokud Claude nějakou akci kvůli oprávnění odmítne,
orchestrátor to zaznamená do výsledku úkolu (`permission_denials`), ale
neudělá to sám za tebe.

## Pracovní prostor (`workspace_root`)

`ClaudeCodeAgent` dostává `cwd` = cesta projektu vrácená
`Config.resolve_project()`. Tahle metoda (a `load_config()` při startu pro
registrované projekty) natvrdo odmítne jakoukoliv cestu, která neleží uvnitř
`workspace_root` - ať je zadaná jménem z `projects` v `config.yaml`, nebo
jako syrová cesta v `--project`. Výchozí `workspace_root` (když není v
`config.yaml` vyplněný) je nadřazený adresář tohoto repozitáře, tedy
`D:\orchestrator` - takže agent smí pracovat v libovolném podadresáři
`D:\orchestrator` (např. `D:\orchestrator\station-agent`), ale nikdy mimo
něj. Cesta registrovaného projektu nemusí předem existovat: `service.py`
(`OrchestratorService.submit`) ji při prvním úkolu založí (`mkdir -p`),
takže agent může založit úplně nový projekt od nuly - `--project` se
syrovou cestou naopak stále vyžaduje, aby adresář už existoval (ochrana
proti překlepu, který by jinak potichu založil adresář kdekoliv v
pracovním prostoru).

Uvnitř `workspace_root` pak o skutečná oprávnění (co smí Claude v daném
projektu upravit/spustit) dál rozhoduje `permission_mode` a `.claude/settings.json`
cílového projektu, jak je popsáno níže - `workspace_root` je jen vnější
hranice "kam vůbec smí sáhnout", ne náhrada za tato jemnější oprávnění.

## Review agent (zatím neaktivní)

Krok "review agent" je v pipeline záměrně jako no-op - žádný druhý agent
zatím není nakonfigurovaný. Až přibude Codex (nebo druhá instance Claude
s jinou rolí), zapojí se jako další krok v `runner.py` mezi testy a commit,
stejným způsobem jako `ClaudeCodeAgent` (přes `orchestrator/agents/base.py`).

## Co tento projekt záměrně NEDĚLÁ (v této fázi)

- Nepushuje nic na GitHub/GitLab/kamkoliv.
- Nezakládá Git repozitář sám od sebe - jen detekuje, jestli projekt Git
  používá, a pokud ne, commit krok přeskočí.
- Nevystavuje API mimo `127.0.0.1`.
- Neimplementuje "Station Agent" ani žádnou další vrstvu nad tímto
  orchestrátorem - to je záměrně mimo rozsah této verze.

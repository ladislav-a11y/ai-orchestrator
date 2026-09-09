# Architektura

## Cílový pracovní tok

```
uživatel / AO
    |
OrchestratorService
    |
provider broker (výběr a nabídka)
    |
AO převezme provider, model a návod z lang*.json
    |
vybraný provider provede úkol
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
- `orchestrator/autonomous.py` - druhý pipeline: autonomní smyčka
  implementace -> testy -> vyhodnocení -> oprava, viz "Autonomní vývojový
  režim" níže.
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
projektu předem povolené nástroje, které agent bude opravdu potřebovat.
Pokud Claude nějakou akci kvůli oprávnění odmítne, orchestrátor to
zaznamená do výsledku úkolu - jak počet (`Task.permission_denials`), tak
konkrétní zamítnuté akce (`Task.permission_denial_details`, převzaté beze
změny z `AgentRunResult.permission_denial_details`/Claude Code JSON
odpovědi), do outbox JSON, do task logu i do výstupu CLI - ale neudělá to
sám za tebe.

### Allow/deny pravidla per projekt (`orchestrator/claude_settings.py`)

Protože nikdo neodklikává interaktivní dotazy, `service.py` (`submit()`) při
založení/prvním sáhnutí na projekt zapíše do `<projekt>/.claude/settings.local.json`
pevně daná allow/deny pravidla (`orchestrator.claude_settings.build_settings()`):
allow pokrývá čtení/úpravu/vytváření souborů projektu, lokální
Python/`.venv`/`pytest`/`python -m unittest` a jen čtecí/stage půlku Gitu
(`init`, `status`, `diff`, `add`, `log` - záměrně BEZ `commit`); deny
natvrdo blokuje `git push`, `git reset --hard`, `git clean -fd`/`-fdx`,
smazání `.git`, přepis historie (`rebase`, `filter-branch`,
`commit --amend`, mazání větví/tagů) a taky `git commit` samotný - commit
smí vytvořit jedině orchestrátor (`runner.py`/`_maybe_commit`,
`autonomous.py`/`_commit_if_ready`), nikdy sám agent (viz AGENTS.md
pravidlo 11 a `NO_COMMIT_INSTRUCTION` v `claude_code.py` pro druhou,
nezávislou vrstvu na úrovni promptu) - to celé je druhá, nezávislá vrstva
vedle `permission_mode` a `FORBIDDEN_*` kontrol výše. Nikdy
soubor nepřepíše, pokud už existuje (ruční úpravy zůstanou zachované), takže
je to jen bezpečné výchozí nastavení pro projekty, které si sám založí.
`doctor` stejná pravidla dodatečně zapíše i do už existujících registrovaných
projektů (`_check_claude_settings`), takže to platí i pro projekty založené
před zavedením tohoto mechanismu.

### Circuit breaker proti opakovaným pokusům o spuštění testů (`orchestrator/hooks/test_command_guard.py`)

Reálné běhy ukázaly, že Claude po prvním zamítnutí testovacího příkazu
(nejčastěji kvůli tomu, že `Bash` nástroj na Windows příkaz obalí přes
`cmd.exe /c "..."`, takže nesedí prefixové `Bash(python:*)`/`Bash(pytest:*)`
allow pravidlo výše) zkoušel jinou variantu (`pytest` → `python -m pytest`
→ `python3 -m pytest` → obalené v `cmd` → ...) klidně 10-20x v jedné session
- zbytečně, protože orchestrátor testy stejně vždy spustí a vyhodnotí sám,
až po skončení úkolu (`runner.py`/`run_test_command`, `autonomous.py`), a
agentovo vlastní spuštění testů na výsledek nemá žádný vliv.

Řešení je `PreToolUse` hook zapsaný do stejného `settings.local.json`
(`build_settings()`, klíč `hooks.PreToolUse`, matcher `"Bash"`) - Claude Code
ho spustí před každým Bash voláním a stdin dostane JSON s `tool_name`/
`tool_input.command`/`session_id`. Hook (ne statický allow/deny seznam výše)
je jediný, kdo rozhoduje o příkazech, které vypadají jako spuštění testů
(`classify_test_command` - pytest/`python -m pytest`/`python -m unittest`/
`nosetests`, hledáno obyčejným `.search()` přes celý řetězec, takže funguje
i skrz `cmd`/`powershell` obal): první takový pokus v dané session dostane
skutečné, vysvětlené zamítnutí (exit kód 2, stderr = důvod), každý další
ekvivalentní pokus je od druhého výskytu okamžitě zkrácen (`MAX_ATTEMPTS_PER_CATEGORY = 1`)
bez dalšího zdůvodňování - stav se drží v `<projekt>/.claude/.test_guard_state/<session_id>.json`.
`ClaudeCodeAgent.run()` po skončení CLI procesu tenhle soubor přečte
(`read_saved_attempts`) a počet uložených pokusů vrátí v
`AgentRunResult.breaker_saved_attempts`; `runner.py`/`autonomous.py` ho
zaloguje a přičtou na `Task.breaker_saved_attempts`/`AutonomousResult.breaker_saved_attempts`,
stejně jako se dnes už loguje `permission_denials` výše. Prompt navíc dostává
`TEST_EXECUTION_INSTRUCTION` (vedle `NO_COMMIT_INSTRUCTION`) - výslovně říká
agentovi, že testy ověřuje jen orchestrátor a že po prvním zamítnutí nemá
zkoušet jinou variantu příkazu.

Selže-li čtení stdin/stavového souboru, hook vždy "fail-open" (exit 0,
povolí) - statická allow/deny pravidla zůstávají záložní vrstvou, stejně
jako u ostatních `permission_mode`/`FORBIDDEN_*` kontrol.

## Volba providera a modelu per požadavek a provider receipt

Navazuje na rešerši v `PROVIDER_MODEL_ROUTING_RESEARCH.md` (kap. 6): AI
Project Manager (nebo jiný volající) předá požadavek brokeru. Broker vlastní
stav providerů, katalogy modelů, pořadí výběru i případné zafixování modelu.
Broker pracovní úkol nespouští; AO od něj převezme nabídku providera, přesný
model a návod z příslušného `lang*.json`, podle kterého vybraného providera
zavolá přímo. Produkční tok proto neobchází broker přímým výběrem providera.

- `AgentRunRequest.requested_model` (`agents/base.py`) - model předaný AO
  brokerem podle jeho nabídky. Prázdná/blank hodnota znamená, že AO přijímá
  model vybraný providerem. Produkční caller model neurčuje mimo broker; adapter
  pouze použije hodnotu, kterou od AO obdržel.
- `AgentRunRequest.selection_reason` - opakní, orchestrátorem
  nevalidovaný důvod volby (např. `"explicit_agent"`, `"default_agent"`,
  nebo PM vlastní klasifikační štítek úlohy). Prochází beze změny až do
  `AgentRunResult.selection_reason`, pokud volání neprošlo failoverem (viz
  níže).
- `AgentRunResult.model`/`model_source` - `model` nese nejlepší dostupnou
  evidenci o skutečně použitém modelu; `model_source` rozlišuje `"reported"`
  (provider to sám potvrdil ve své JSON odpovědi), `"reported_receipt"`
  (přesný model z platného receipt, pokud metadata chybí), `"requested"` (jen víme,
  že jsme poslali `requested_model`, provider to nepotvrdil) a
  `"configured"` (poslali jsme statickou `config.yaml` hodnotu, opět
  nepotvrzenou). `None`/`None`, když provider nic neposlal ani nic
  nekonfiguroval - hodnota se nikdy nevymýšlí (viz `claude_code.py`'s
  `_reported_model()`, který u Claude Code záměrně NIKDY nepoužije
  `requested`/`configured` fallback, protože provider smí zvolit model
  sám i když `--model` nedostal).
- `FailoverAgent._compose_selection_reason()` (`agents/failover.py`) - když
  výsledné volání proběhlo na prvním zkoušeném provideru, důvod se jen
  echuje z `request.selection_reason`; jakmile failover přeskočil alespoň
  jednoho providera (LIMITED/nedostupný/timeout/...), sestaví strojově
  čitelný řetězec `"failover: <status providera 1>; ... -> <aktivní
  provider>"` ze stejných dat, která už `provider_status_snapshot()`
  poskytuje - volající tak nikdy nemusí ručně rekonstruovat důvod z mapy
  `provider_statuses`.
- `Task.requested_model`/`selection_reason`/`model`/`model_source`
  (`models.py`) - `requested_model`/`selection_reason` nastaví volající
  (CLI/API/Inbox) před spuštěním, `runner.py`'s `run_task()` po běhu (i po
  každém fix-pokusu) přepíše `model`/`model_source`/`selection_reason`
  skutečnou hodnotou z `AgentRunResult`. Protože `Task.to_dict()` serializuje
  `__dict__` beze změny, `outbox/<task_id>.json` (běžný `run`/
  `import-inbox` úkol) tuto čtveřici automaticky obsahuje - odstraňuje to
  asymetrii popsanou v rešerši (kap. 5.3), kdy jednorázový outbox neměl
  žádný ekvivalent autonomního `active_provider`/`active_model`.
- `OrchestratorService.submit()` - když volající `selection_reason`
  nezadá, doplní ho sám: `"explicit_agent"`, když byl zadán `agent_name`,
  jinak `"default_agent"` (padlo se na `Config.default_agent`) - takže tahle
  distinkce nikdy nechybí ani u nejjednoduššího volání bez explicitního
  důvodu.
- `import_inbox()` čte `requested_model`/`selection_reason` z JSON souboru v
  `inbox/` stejně nevalidovaně jako `submit()`.

Mimo rozsah (viz rešerše kap. 6 bod 5 a `inbox_planning_recipe.md`):
klasifikace úlohy podle typu/složitosti zůstává vlastnictvím AI Project Manageru,
ale vlastní výběr dostupného providera a modelu v produkčním toku provádí broker.
Samostatný, dřívější mechanismus
`OrchestratorService.run_autonomous(..., model_override=...)` (per-run
override modelu pro autonomní smyčku přes dočasně nahrazenou kopii
`Config`, nikdy ne mutaci originálu) zůstává beze změny vedle tohoto
kontraktu - řeší jiný vstupní bod (celý autonomní běh, ne jedno
`AgentRunRequest`) a nesdílí s ním datový tok.

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

## Autonomní vývojový režim (`orchestrator autonomous`)

Druhý, samostatný pipeline vedle jednorázového `run`/`Task` toku výše -
implementovaný v `orchestrator/autonomous.py`, zapojený do `service.py`
metodou `run_autonomous()` a do CLI příkazem `autonomous` (`cli.py`).
Cílem je opakovat cyklus **implementace -> testy -> vyhodnocení -> oprava**,
dokud není splněná uživatelem zadaná Definition of Done (DoD), nebo dokud
není dosažen bezpečný limit iterací:

```
projekt + cíl + Definition of Done
    |
    +--> iterace 1..N (max. `max_iterations`, natvrdo omezeno na
    |     ABSOLUTE_MAX_ITERATIONS bez ohledu na to, co si uživatel zadá):
    |       1. vyber dávku (`_select_batch`, max. DOD_BATCH_SIZE=8) aktuálně
    |          nesplněných bodů DoD - ne celý seznam, viz "Dávkování a
    |          úsporný kontext" níže. Pokud je dávka prázdná (executor už
    |          tvrdí, že je hotovo), přeskoč rovnou na krok 3.
    |       2. sestav kompaktní prompt (cíl, jen tato dávka bodů, `git
    |          status`, OVĚŘENÝ výsledek testů z minulé iterace, poznámka
    |          agenta z minulé iterace) a spusť agenta (stejné `Agent.run()`
    |          rozhraní jako `runner.py`, session se navazuje přes
    |          `--resume`). Pokud odpověď neodpovídá JSON kontraktu, zkus
    |          přesně jeden levný "repair" reprompt (viz níže) - ne novou
    |          plnou implementační iteraci.
    |       3. spusť testy - pokud má projekt/config nastavený test_command,
    |          spustí se vždy (stejná funkce `runner.run_test_command`, která
    |          vždy vrátí skutečný bool, nikdy None); pokud test_command
    |          nastavený není, `_detect_test_command()` se pokusí odhadnout
    |          rozumný výchozí příkaz (`python -m pytest -q`, jen pokud
    |          projekt vypadá jako Python projekt se skutečnou složkou
    |          `tests/`) - `tests_passed=None` zůstává jen pro projekty, kde
    |          žádný testovací příkaz skutečně nedává smysl ani po této
    |          detekci
    |       4. pokud executor tvrdí, že JSOU splněny všechny body DoD A
    |          testy prošly A odpověď byla protokolově v pořádku, spusť
    |          nezávislou audit kontrolu (`_run_audit`, viz "Audit"
    |          níže) - teprve její potvrzení otevírá cestu ke commitu.
    |       5. zaloguj iteraci (logger + průběžný soubor
    |          logs/autonomous/<id>.log), včetně velikosti promptu ve
    |          znacích/odhadu tokenů (viz "Logování")
    |
    +--> completed:  všechny body DoD splněné A testy prošly (nebo žádné
    |                 testy nejsou nastavené/detekované) A audit nic
    |                 neodmítl -> pokus o Git commit za stejných podmínek
    |                 jako `runner._maybe_commit` (auto_commit zapnutý,
    |                 testy neselhaly, je co commitnout)
    +--> blocked:     stejný OVĚŘENÝ stav (stejné nesplněné body DoD + stejný
    |                 skutečně naměřený výsledek testů) se opakuje
    |                 `NO_PROGRESS_LIMIT` (3) iterací po sobě bez posunu -
    |                 iterace s protokolovou chybou (viz níže, i po repair
    |                 pokusu) nebo s neparsovatelnou audit odpovědí se do
    |                 této detekce nezapočítávají
    +--> max_iterations: vyčerpán limit iterací, DoD stále nesplněná
    +--> budget_exceeded: aktivní provider překročil svůj nakonfigurovaný
    |                 `max_budget_usd` PRO TENTO BĚH a žádný další
    |                 nakonfigurovaný provider nebyl k dispozici (viz
    |                 "Per-job finanční limit" níže)
    +--> error:       samotné volání agenta selhalo (chyba/timeout) - loop
                      se hned zastaví, nezkouší to slepě znovu
```

### Per-job finanční limit (`max_budget_usd`, provider-specific)

Doplněk k `max_iterations`/`ABSOLUTE_MAX_ITERATIONS` (hard cap na *počet*
iterací): `ClaudeCodeAgentConfig`/`AntigravityAgentConfig`/
`CodexAgentConfig.max_budget_usd` (viz `config.example.yaml`) je hard cap na
*útratu* jednoho providera během jednoho autonomního běhu, v USD. `None`
(výchozí) = bez limitu.

- Kontroluje se jednou po každé iteraci (`_provider_budget_usd` v
  `autonomous.py`), proti kumulativnímu `cost_usd` reportovanému tímto
  providerem NAPŘÍČ celým tímto během (`AutonomousResult.usage_by_provider`)
  - ne proti nákladu jediného volání. Chybějící/nereportovaný `cost_usd`
  (usage tracking je best-effort) tento limit nikdy nespustí - kontrola
  reaguje jen na pozitivně potvrzenou útratu.
- Když je limit překročen: pokud agent podporuje failover
  (`FailoverAgent.force_failover_on_budget_exceeded()`, stejný mechanismus
  jako u `force_failover_on_protocol_error()`), běh přepne na dalšího
  nakonfigurovaného providera a pokračuje. Pokud žádný další provider
  nezbývá (nebo je agent jednoduchý, bez failoveru), běh se zastaví se
  stavem `AutonomousStatus.BUDGET_EXCEEDED` a pošle se Slack notifikace -
  nikdy nepokračuje "naslepo" za nakonfigurovaný strop.
- Odlišeno od `WAITING_FOR_PROVIDER` (`ProviderStatus.limited` - kvóta/rate
  limit reportovaný providerem samotným): `ProviderStatus.budget_exceeded`
  je vlastní finanční strop, který si nastavuje a hlídá orchestrátor, ne
  signál od providera.

### Root cause: run `11b4aaae08b4` (66 bodů DoD, 8 ztracených iterací)

Tento běh měl 8 iterací za sebou s `tests_passed=True`, ale každá byla
zaznamenaná jako `protocol_error=True`, takže se 66 bodů DoD tvářilo jako
stále nesplněných a spotřeboval se session limit bez jediného zaznamenaného
pokroku. Skutečná odpověď agenta byla naprosto validní JSON - problém byl v
`ClaudeCodeAgent.run()` (`orchestrator/agents/claude_code.py`): když Claude
Code CLI vrátilo `permission_denials`, wrapper připojil čitelnou poznámku
(`"\n\n[orchestrator] Claude odmítl N akcí(í) kvůli oprávněním."`) přímo za
`result_text`, tedy za agentův vlastní JSON payload. Starý `_extract_json`
dělal naivní `json.loads` na celý text (případně na obsah markdown bloku),
takže cokoliv za validním JSON objektem parsování celé rozbilo. Oprava má
dvě části: (1) wrapper už tuto poznámku do `output_text` nepřidává - počet
odmítnutí se místo toho vrací strukturovaně v novém poli
`AgentRunResult.permission_denials`; (2) `_extract_json` je teď odolný i
bez toho - skenuje text na vyvážené `{...}` objekty a zkouší poslední
takový nejdřív, takže zvládne JSON obalený libovolnou prózou nebo
poznámkou před/za sebou (viz `_iter_balanced_objects`).

### Dávkování a úsporný kontext

Aby jedna iterace nemusela vypsat ani vyžádat JSON pro *všechny* body DoD
najednou (u specifikace se 66 body to dělalo obří prompty i obří
požadované odpovědi - a čím větší odpověď, tím větší riziko, že se něco
upytlíkuje/zkrátí), `_select_batch` vybere vždy jen prvních
`DOD_BATCH_SIZE` (8) aktuálně nesplněných bodů. `_build_iteration_prompt`
pak vypíše jen tuto dávku (zbytek jen jako souhrnné číslo "X/Y splněno") a
kontrakt vyžaduje JSON záznam jen pro indexy z této dávky - `_apply_dod_updates`
kontroluje pokrytí právě vůči `requested_indices`, ne vůči celkovému počtu
položek DoD. Pokud jsou už všechny body podle dosavadního stavu splněné,
dávka je prázdná a iterace rovnou přeskočí volání implementačního agenta
(jde jen znovu spustit testy a audit) - viz "Audit" níže. Prompt navíc
posílá jen OVĚŘENÝ výsledek testů z předchozí iterace a poznámku agenta,
ne celý předchozí kontext - conversation session se navazuje přes
`--resume`, takže historie samotná se neztrácí.

### Kontrakt agent <-> orchestrátor (JSON vyhodnocení)

Protože orchestrátor nemá jinou cestu, jak zjistit, které body DoD jsou
splněné, než se zeptat samotného agenta, každý prompt v `_build_iteration_prompt`
explicitně žádá, aby úplně poslední zpráva agenta byla výhradně jeden JSON
objekt tvaru `{"items": [{"index": 0, "done": true}, ...], "notes": "..."}`
- jeden záznam pro každý index z aktuální dávky (viz výše). `_extract_json`
to parsuje (i přes případný markdown blok nebo okolní text),
`_apply_dod_updates` promítne výsledek do stavu - ale nikdy mu neslepě
nevěří:

- **Merge je monotónní.** Položka jednou ověřená jako splněná (`done=True`)
  už nemůže být pozdější (méně pečlivou) odpovědí *executor* agenta vrácena
  zpátky na nesplněnou - `done = puvodni_done or tvrzeni_agenta`. Jedinou
  výjimkou je nezávislý audit pass (viz níže), který smí falešně tvrzený
  bod znovu otevřít.
- **Neplatný/neúplný JSON je "protocol error", ne "no progress".** Pokud
  odpověď není JSON, nemá pole `items`, neobsahuje záznam pro každý
  požadovaný index z dávky, nebo obsahuje položku se špatným/mimo rozsah
  indexem, `_apply_dod_updates` vrátí `protocol_error=True` (a seznam
  chybějících indexů). V tom případě `run_autonomous_loop` zkusí přesně
  jeden levný "repair" reprompt (`_build_repair_prompt`) - krátká zpráva,
  která NEOPAKUJE cíl/DoD/git status, jen žádá agenta, aby beze změn kódu
  znovu poslal JSON pro chybějící indexy. Pokud repair uspěje,
  `protocol_error` se pro danou iteraci vrátí na `False`; pokud ne, zůstává
  `True` a do poznámky pro příští iteraci se přidá konkrétní hint.
  `IterationLog.protocol_error`/`repair_attempted`/`repair_succeeded` to
  zaznamenají i do `outbox/autonomous-<id>.json` pro dohledatelnost.
- **Finální "completed" stav nezávisí na tvrzení agenta o testech ani o
  splnění DoD.** I když agent v JSON tvrdí, že testy prošly a vše je
  hotovo, `completed` se rozhoduje podle skutečného `tests_passed` z
  orchestrátorova vlastního spuštění testů (bod 3 výše) A podle nezávislého
  auditu (viz níže) - nikdy jen podle agentova textu.

### Audit (Manager/Executor/Auditor)

Inspirováno principem Manager-Executor-Auditor z LongHorizon Harness (bez
jakékoliv závislosti na `lh-harness` - jde jen o druhý prompt proti
stejnému `Agent`). Jakmile v jedné iteraci executor tvrdí, že jsou splněny
úplně všechny body DoD, orchestrátorovy testy souhlasí, A odpověď byla
protokolově v pořádku, `_run_audit` pošle samostatný prompt (označený
`AUDIT_MARKER`), který explicitně zakazuje jakoukoliv úpravu kódu a žádá
jen nezávislé ověření každého bodu, s odpovědí
`{"rejected_indices": [...], "notes": "..."}`:

- pokud audit vrátí neparsovatelnou odpověď, běh se nepovažuje za
  dokončený, ale ani se to nepočítá jako "no progress" (stejná logika jako
  `protocol_error` u executor kontraktu) - zkusí se to znovu příští
  iteraci;
- pokud audit něco odmítne, dané body DoD se vrátí na `done=False`
  (jediná výjimka z monotónního merge popsaného výše) a běh pokračuje;
- pokud audit nic neodmítne, teprve pak proběhne pokus o commit.

Auditor nikdy nic neimplementuje - je to čistě ověřovací krok, ne druhý
pokus o řešení úkolu.

### Detekce "bez pokroku" (blocked)

`_iteration_signature()` spočítá otisk z (nesplněné body DoD, výsledek
testů, konec výstupu testů) - ale tato detekce se počítá jen pro iterace se
skutečně ověřitelným stavem. Iterace, kde `_apply_dod_updates` vrátila
`protocol_error=True` (i po repair pokusu), kde audit vrátil neparsovatelnou
odpověď, nebo kde je nastavený test_command a `tests_passed` přesto vyšlo
`None` (nemělo by nastat po opravě z run 9cd54b5bf219, ale kontrola
zůstává jako pojistka), se do porovnání vůbec nezapočítá - ani jako
"stejný stav", ani jako reset čítače. Teprve pokud se otisk
`NO_PROGRESS_LIMIT` (3) OVĚŘENÝCH iterací po sobě nezmění, běh se ukončí
jako `blocked`. Bez tohoto rozlišení by tři po sobě jdoucí nezparsovatelné
odpovědi agenta (nebo tři iterace bez skutečně spuštěných testů) vypadaly
jako "stejná chyba pořád dokola", i když orchestrátor ve skutečnosti žádný
srovnatelný signál nezískal - přesně tato záměna byla druhou příčinou toho,
že běh `9cd54b5bf219` skončil jako `blocked` misto pokračování/max_iterations.

### Opakovaná protokolová chyba nesmí běžet donekonečna (incident 7fffd21835174d9fb9a29237c897f6d2)

Vyloučení protokolové chyby z detekce "bez pokroku" (výše) je záměrné, ale
nesmí se stát dírou, kterou lze utéct do nekonečného opakování. V produkčním
běhu `7fffd21835174d9fb9a29237c897f6d2` (26. 8. 2026) Codex v iteracích 1-7
vždy provedl reálné změny a testy prošly, ale ani jednou nevrátil platný
finální DoD JSON; jediný levný repair pokus selhal také 7x za sebou.
Protokolová chyba se do `NO_PROGRESS_LIMIT` nepočítá, takže orchestrátor
7x za sebou zahájil novou plnou vývojovou iteraci se stejným nezměněným DoD,
dokud Codex nevyčerpal usage limit - bez jediného zaznamenaného přínosu.

Řešení: `run_autonomous_loop` počítá samostatný čítač `protocol_error_streak`
- kolik iterací PO SOBĚ skončilo s `protocol_error=True` (i po repair pokusu)
nebo `audit_protocol_error=True`. Jakmile dosáhne `PROTOCOL_ERROR_STREAK_LIMIT`
(2, záměrně nižší než `NO_PROGRESS_LIMIT`):

- pokud `agent` (typicky `FailoverAgent`, viz `orchestrator/agents/failover.py`)
  poskytuje metodu `force_failover_on_protocol_error(reason)`, zavolá se -
  opakovaná protokolová nekompatibilita dostává stejné právo na failover na
  dalšího nakonfigurovaného providera jako vyčerpaná kvóta/limit. Pokud
  failover uspěje (existuje další provider), čítač se vynuluje a běh
  pokračuje s novým providerem;
- jinak (žádný failover k dispozici, nebo šlo o posledního providera) se běh
  rovnou zastaví se stavem `AutonomousStatus.PROTOCOL_ERROR` - NEPOKRAČUJE
  se do další plné iterace se stejným nezměněným DoD.

Zastavení/failover pošle jasný důvod přes `slack_notify.notify()` a
`AutonomousResult` nese `protocol_error_total`/`protocol_error_wasted_prompt_chars`
(hrubý odhad promarněných tokenů - `znaky // 4`, stejné jednotky jako jinde v
logování), takže `outbox/autonomous-<run_id>.json` (a tedy i AI Project
Manager/Trello/Slack handoff) vidí, kolik iterací/tokenů protokolová chyba
stála, i na běhu, který nakonec neskončil jako `PROTOCOL_ERROR`. Checkpoint
(`autonomous_checkpoint.py`) i dosud ověřené DoD položky a výsledky testů
zůstávají zachované beze změny - stop/failover nikdy neoznačí nic za
splněné, jen přestane slepě opakovat stejný neúspěšný protokol.

### Logování

Každá iterace se loguje na dvou místech: (1) `logger.info`/`warning` do
sdíleného `logs/orchestrator.log` (stejný logger jako zbytek orchestrátoru)
- včetně velikosti promptu dané iterace ve znacích a hrubého odhadu počtu
tokenů (`znaky // 4`), i běžícího součtu za celý běh, aby byla spotřeba
kontextu/tokenů vidět bez nutnosti to dopočítávat zpětně z logu -
(2) plný přepis (zadání, výstup agenta, výstup testů, poznámka) do
`logs/autonomous/<run_id>.log`, přepisovaný po každé iteraci (`on_iteration`
callback v `service.py`), takže i běh přerušený uprostřed nechá na disku
kompletní záznam všech dosavadních iterací. Strojově čitelný výsledek
(stav, DoD položky, všechny iterace včetně `prompt_chars`,
`requested_indices`, `repair_attempted`, `audit_performed`) jde do
`outbox/autonomous-<run_id>.json`, stejně jako `outbox/<task_id>.json` u
běžných úkolů.

### Vyčerpání všech providerů (`WAITING_FOR_PROVIDER`, incident cb501524e47e, 26.8.2026)

Když `FailoverAgent` (viz `agents/failover.py`) vyčerpá celé nakonfigurované
pořadí providerů - každý je buď lokálně nedostupný, nebo vrátil
`limited=True` - `run_autonomous_loop` běh **neukončí jako chybu**, ale jako
`AutonomousStatus.WAITING_FOR_PROVIDER` s `retry_after_seconds` převzatým z
nejbližšího známého resetu (`min()` přes všechny providery, které svůj reset
znají). `OrchestratorService.run_autonomous` z toho uloží do fronty
čekající task (`TaskStatus.WAITING_FOR_PROVIDER`) s `retry_at`, `spec_text` a
`run_id` zachovaným kvůli navázání - a `autonomous_checkpoint.py` už dřív
uložil, které body Definition of Done jsou ověřené hotové, takže žádný
následný běh nezačíná od bodu 0.

Produkční incident 26.8.2026 (task `cb501524e47e`): Claude LIMITED (reset
22:10 Europe/Prague), Antigravity LIMITED (reset cca +70h36m), Codex LIMITED
(reset 22:05) - orchestrátor čekající task uložil správně, ale AI Project
Manager viděl jen `status=in_progress` a nedostal žádnou Slack notifikaci,
protože (a) Slack zpráva při vyčerpání providerů hlásila jen jejich jména,
ne stav/reset každého zvlášť, (b) neexistovala žádná run-úrovňová
notifikace o tom, že běh čeká, a (c) nic neřeklo, jestli se běh sám obnoví.
Oprava:

- `FailoverAgent.run()` při vyčerpání pošle přes `slack_notify.notify()`
  zprávu se stavem KAŽDÉHO providera zvlášť (LIMITED s časem resetu /
  nedostupný s důvodem) a nejbližším známým reset/retry
  (`_describe_status_for_notify`/`_format_duration`).
- `OrchestratorService.run_autonomous` navíc pošle run-úrovňovou Slack
  notifikaci (projekt, `run_id`, `retry_after_seconds`) při přechodu do
  `WAITING_FOR_PROVIDER`.
- `OrchestratorService.auto_resume_active` (nastaveno konstruktorovým
  parametrem `persistent`) rozlišuje, jestli TENTO proces sám čekající běh
  obnoví - `True` jen pro dlouhoběžící proces (`orchestrator.py api`, viz
  `api.py`), `False` pro jednorázový `orchestrator.py autonomous`/`run`
  (skutečný způsob, jak AI Project Manager tento orchestrátor spouští - viz
  README kap. 9). Hodnota jde do `outbox/autonomous-<run_id>.json` jako
  `auto_resume_active` i do CLI výstupu, takže se nikdy nepředstírá
  automatické pokračování, které neběží.
- `OrchestratorService.run_autonomous` teď hledá a uzavírá existující
  čekající frontový záznam (`find_active_autonomous`) i když běh doběhne
  přes zcela NOVOU instanci `OrchestratorService` (skutečný produkční
  případ - každé volání CLI je jiný proces), ne jen přes interní
  `_waiting_worker`. Bez toho by frontový záznam zůstal navždy trčet na
  `WAITING_FOR_PROVIDER`, i když běh mezitím dokončila samostatná
  invokace CLI - přesně ten typ falešného/zastaralého stavu, který DoD
  zakazuje.

### Definition of Done

Uživatel zadá `--goal` (volný text, kontext cíle) a/nebo `--spec` (cesta k
souboru s DoD). `parse_definition_of_done()` v `autonomous.py` bere jako
položku DoD VÝHRADNĚ řádky ve tvaru checklistu (`- [ ] ...` / `- [x] ...`,
i s `*` místo `-` - stav zaškrtnutí se stane počátečním stavem položky).
Markdown nadpisy (`# ...`, `## ...`), prázdné řádky a jakýkoli jiný text se
NIKDY nestanou položkou - reálný spec soubor jako `dod-station-agent-v1.md`
kombinuje nadpisy sekcí s checklistem a nadpis nemůže být nikdy "splněný";
kdyby se parsoval jako položka, DoD by nikdy nemohla být kompletně splněná
(přesně tohle způsobilo, že běh `9cd54b5bf219` skončil jako `blocked`, viz
git historie/commit message opravy). Spec bez jediného checklist řádku
(typicky samotný `--goal` bez `--spec`) padá zpět na jednu položku, která
drží celý text, takže i běh jen s `--goal` má vždy >=1 položku.

### Kontrakt agent <-> orchestrátor (JSON vyhodnocení)

Protože orchestrátor nemá jinou cestu, jak zjistit, které body DoD jsou
splněné, než se zeptat samotného agenta, každý prompt v `_build_iteration_prompt`
explicitně žádá, aby úplně poslední zpráva agenta byla výhradně jeden JSON
objekt tvaru `{"items": [{"index": 0, "done": true}, ...], "notes": "..."}`
- jeden záznam pro každou DoD položku. `_extract_json` to parsuje (i přes
případný markdown blok), `_apply_dod_updates` promítne výsledek do stavu -
ale nikdy mu neslepě nevěří:

- **Merge je monotónní.** Položka jednou ověřená jako splněná (`done=True`)
  už nemůže být pozdější (méně pečlivou) odpovědí agenta vrácena zpátky na
  nesplněnou - `done = puvodni_done or tvrzeni_agenta`. Skutečně splněno
  napříč iteracemi zůstává zachováno.
- **Neplatný/neúplný JSON je "protocol error", ne "no progress".** Pokud
  odpověď není JSON, nemá pole `items`, má jiný počet položek než DoD, nebo
  obsahuje položku se špatným/mimo rozsah indexem, `_apply_dod_updates`
  vrátí `protocol_error=True` a do poznámky pro příští iteraci přidá
  konkrétní hint, co bylo špatně - agent dostane šanci to v další iteraci
  opravit. `IterationLog.protocol_error` to zaznamená i do
  `outbox/autonomous-<id>.json` pro dohledatelnost.
- **Finální "completed" stav nezávisí na tvrzení agenta o testech.** I když
  agent v JSON tvrdí, že testy prošly, `completed` se rozhoduje podle
  skutečného `tests_passed` z orchestrátorova vlastního spuštění testů
  (bod 3 výše), ne podle agentova textu.

### Detekce "bez pokroku" (blocked)

`_iteration_signature()` spočítá otisk z (nesplněné body DoD, výsledek
testů, konec výstupu testů) - ale tato detekce se počítá jen pro iterace se
skutečně ověřitelným stavem. Iterace, kde `_apply_dod_updates` vrátila
`protocol_error=True`, nebo kde je nastavený test_command a `tests_passed`
přesto vyšlo `None` (nemělo by nastat po opravě z run 9cd54b5bf219, ale
kontrola zůstává jako pojistka), se do porovnání vůbec nezapočítá - ani
jako "stejný stav", ani jako reset čítače. Teprve pokud se otisk
`NO_PROGRESS_LIMIT` (3) OVĚŘENÝCH iterací po sobě nezmění, běh se ukončí
jako `blocked`. Bez tohoto rozlišení by tři po sobě jdoucí nezparsovatelné
odpovědi agenta (nebo tři iterace bez skutečně spuštěných testů) vypadaly
jako "stejná chyba pořád dokola", i když orchestrátor ve skutečnosti žádný
srovnatelný signál nezískal - přesně tato záměna byla druhou příčinou toho,
že běh `9cd54b5bf219` skončil jako `blocked` misto pokračování/max_iterations.

### Logování

Každá iterace se loguje na dvou místech: (1) `logger.info`/`warning` do
sdíleného `logs/orchestrator.log` (stejný logger jako zbytek orchestrátoru),
(2) plný přepis (zadání, výstup agenta, výstup testů, poznámka) do
`logs/autonomous/<run_id>.log`, přepisovaný po každé iteraci (`on_iteration`
callback v `service.py`), takže i běh přerušený uprostřed nechá na disku
kompletní záznam všech dosavadních iterací. Strojově čitelný výsledek
(stav, DoD položky, všechny iterace) jde do
`outbox/autonomous-<run_id>.json`, stejně jako `outbox/<task_id>.json` u
běžných úkolů.

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

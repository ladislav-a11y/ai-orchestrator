# Rešerše: schopnosti providerů a modelů pro směrování podle typu/složitosti úlohy

> Historický návrhový podklad a snapshot stavu k 2026-09-05.
> Aktuální provozní kontrakt, skutečné zdroje nabídky a fail-closed postup pro AI
> Project Manager jsou v `PROVIDER_MODEL_CAPABILITIES.md` a
> `PROVIDER_BROKER_GUIDE.md`. Následující historická tvrzení o chybějícím
> rozhraní se nesmějí používat jako popis současného v2 chování.

Datum: 2026-09-05
Rozsah: čistě historická rešeršní karta AI Orchestratoru (viz
`orchestrator/inbox_planning_recipe.md`, hranice 1-2). Při jejím vzniku
neimplementovala směrování podle typu/složitosti úlohy ani jeho zobrazení v
notifikacích. Tento dokument sám nemění běhové chování a jeho původní závěry
nejsou aktuálním v2 kontraktem.

## Aktuální v2 stav

V2 nyní používá deterministický profil konkrétního úkolu, nikoli pevné mapování
workflow fáze na model. `orchestrator/model_routing.py` odvozuje z textu úkolu
a DoD zejména `work_type`, `complexity` a `model_tier`. AO dispatch tento profil
předává brokeru jako součást `select_provider`. Broker potom u placených
providerů (`claude-code`, `codex`) vybere z potvrzeného katalogu přesné ID
modelu. Free provideři (`groq`, `antigravity`) se tímto dynamickým výběrem
nemění. Persistentní uživatelský výběr modelu má přednost před profilem.

Podrobný aktuální kontrakt je v `PROVIDER_BROKER_GUIDE.md`, zejména v části
`Task-driven model routing v2`. Implementaci tvoří také
`orchestrator/broker_dispatch.py` a výběr katalogového modelu v
`orchestrator/provider_broker.py`.

## 1. Co "provider" v tomto projektu znamená

AI Orchestrator nevolá žádné LLM API přímo. Každý "provider" je adaptér nad lokálně
nainstalovaným CLI nástrojem, který sám interně vybírá/volá model:

| Provider (`Agent.name`) | Adaptér | CLI nástroj | Config třída |
|---|---|---|---|
| `claude-code` | `orchestrator/agents/claude_code.py` | `claude` (Claude Code CLI) | `ClaudeCodeAgentConfig` |
| `antigravity` | `orchestrator/agents/antigravity.py` | `agy` (Antigravity CLI) | `AntigravityAgentConfig` |
| `codex` | `orchestrator/agents/codex.py` | `codex exec` (OpenAI Codex CLI) | `CodexAgentConfig` |
| `gemini` | `orchestrator/agents/gemini.py` | `gemini` (Gemini CLI) | `GeminiAgentConfig` |
| `auto`/`failover` | `orchestrator/agents/failover.py` (`FailoverAgent`) | obaluje výše uvedené čtyři v pořadí | `Config.provider_order` |

Zdroj pravdy pro seznam podporovaných providerů je `config.AVAILABLE_AGENTS =
["claude-code", "antigravity", "codex", "gemini"]` a `registry.build_agent()`
(`orchestrator/agents/registry.py:19`), který jméno providera mapuje na konkrétní třídu.
Přidání nového providera je jedna nová třída implementující `Agent` (`agents/base.py`)
plus jeden řádek v registru - nic jiného v orchestrátoru se nemusí měnit (viz komentář
v `registry.py:1-7`).

## 2. Podporované modely per provider

**Žádný provider adaptér neudržuje katalog podporovaných modelů.** Model je buď:

- **explicitně nakonfigurovaný** řetězec v `config.yaml` (`<provider>.model`), předaný
  CLI nástroji jako `--model <hodnota>` (viz `claude_code.py:284-285`,
  `antigravity.py:241-242`, `codex.py:337-338`, `gemini.py:209-210`) - orchestrátor
  hodnotu nijak nevaliduje proti seznamu známých modelů, jen ji předá dál; platnost
  ověří samotné CLI při spuštění, nebo
- **prázdný** (`model: ""`), což znamená "nech CLI nástroj zvolit svůj vlastní
  výchozí model" - orchestrátor v tomto případě `--model` vůbec nepřidá do příkazu
  (`if self.config.model: cmd += ["--model", ...]`).

Jediná výjimka je Gemini: `GeminiAgentConfig.model` má netriviální výchozí hodnotu
`GEMINI_FREE_MODEL = "gemini-2.5-flash"` (`config.py:47,120`) a `--model` se u něj
předává vždy (`gemini.py:209-210`), protože jde záměrně o free-tier provider s
deterministickým modelem pro headless PM běhy (viz komentář `gemini.py:1-11`).

Žádné metadata o schopnostech modelu (kontextové okno, cena, rychlost, vhodnost pro typ
úlohy) nejsou nikde v repozitáři reprezentována - ani jako konstanta, ani jako
konfigurační pole, ani jako komentář s doporučením. Neexistuje tedy dnes žádný
strojově čitelný podklad, ze kterého by šlo odvodit "tenhle model/provider je vhodný
pro složitou/dlouhou úlohu, tenhle pro jednoduchou".

## 3. Výběr providera: explicitní vs. automatický

### 3.1 Explicitní výběr

- CLI: `orchestrator.py run --agent <claude-code|antigravity|codex|gemini|auto>`
  (`cli.py`), `orchestrator.py autonomous --agent ...` - stejná sada hodnot.
- API/`Task.agent`: pole `agent` na `Task` (`models.py:40`, default `"claude-code"`).
- `Config.default_agent` (`config.py:161`, výchozí `"claude-code"`) - použije se, když
  volající agenta nezadá.

Explicitní volba vždy vybírá **jednoho konkrétního providera**; model uvnitř něj je
pořád jen to, co je nakonfigurované v `config.yaml` pro daného providera (viz výše) -
neexistuje CLI/API parametr pro přepsání modelu per-požadavek (per-task), jen per-provider
v config.yaml.

### 3.2 Automatický výběr (`--agent auto`/`failover`)

`FailoverAgent` (`orchestrator/agents/failover.py`) implementuje **jediný typ
"automatiky", který dnes existuje**: statické pořadí providerů
(`Config.provider_order`, výchozí `["gemini", "antigravity", "claude-code", "codex"]`,
`config.py:53`), zkoušené popořadě. Přepnutí na dalšího providera v pořadí nastává
výhradně kvůli:

- lokální nedostupnosti (`provider.is_available()` vrátí `False` - CLI nenalezeno,
  nefunkční autentizace apod.),
- `AgentRunResult.limited=True` (kvóta/rate/session limit reportovaný providerem),
- `AgentRunResult.timed_out=True` nebo `unavailable=True`,
- `force_failover_on_protocol_error()` (opakovaně neplatný JSON kontrakt od agenta),
- `force_failover_on_budget_exceeded()` (překročen `max_budget_usd` pro tento běh),
- `force_failover_on_audit_quality()` (věcně nedostatečný audit - jen pro audit roli).

Historický snapshot k 2026-09-05: toto pořadí a přepínání tehdy nemělo žádný
vztah k typu nebo složitosti zadané úlohy.
`FailoverAgent.run()` (`failover.py:323`) nepřijímá a nikde nevyhodnocuje žádnou
charakteristiku úlohy (délku, doménu, odhad obtížnosti) - jediný vstup je
`AgentRunRequest` (prompt, cesta k projektu, `session_id`, `context`, `output_schema`).
Volba modelu uvnitř aktivního providera je pořád jen jeho statická config hodnota z
bodu 2 - `FailoverAgent` model nijak neovlivňuje ani nepředává.

Historické shrnutí: tehdejší routing byl čistě failover kvůli
dostupnosti/kvótě. Toto už není popis současného v2 stavu; aktuální profilový
routing je popsán v části `Aktuální v2 stav` výše.

## 4. Relevantní rozhraní

- `orchestrator/agents/base.py`
  - `Agent` (abstraktní): `is_available() -> (bool, str)`, `run(AgentRunRequest) ->
    AgentRunResult`. Toto je jediný kontrakt, který nový/měněný provider musí splnit.
  - `AgentRunRequest`: `project_path`, `prompt`, `context`, `session_id`,
    `output_schema`. **Neobsahuje pole pro model ani pro "důvod volby".**
  - `AgentRunResult`: `success`, `output_text`, `raw_response`, `session_id`,
    `cost_usd`, `error`, `permission_denials(_details)`, `breaker_saved_attempts`,
    `input_tokens`/`output_tokens`/`thinking_tokens`/`total_tokens`, `usage_events`,
    `limited`, `timed_out`, `unavailable`, `retry_after_seconds`, **`model`**.
- `orchestrator/agents/registry.py` - `build_agent(name, config)`,
  `build_failover_agent(config, provider_order=None, ...)`.
- `orchestrator/config.py` - `AVAILABLE_AGENTS`, `DEFAULT_PROVIDER_ORDER`,
  `Config.default_agent`, `Config.provider_order`, per-provider `*AgentConfig.model`.
- `orchestrator/agents/failover.py` - `FailoverAgent`, `ProviderStatus`,
  `provider_status_snapshot()`, `force_failover_on_*()`.
- `orchestrator/models.py` - `Task.agent` (jméno providera pro daný úkol).
- `orchestrator/service.py` - `_write_autonomous_outbox()` sestavuje finální
  `outbox/autonomous-<run_id>.json` (viz bod 5).

## 5. Současná podoba "provider receipt"

Receipt = důkaz o tom, který provider/model úlohu skutečně provedl. Existuje na dvou
úrovních:

### 5.1 Per-volání (`AgentRunResult.model`)

Každý adaptér se snaží dohledat **skutečně reportovaný** model z JSON odpovědi CLI, a
teprve když provider žádnou evidenci nevrátí, spadne (u antigravity/codex/gemini) na
nakonfigurovanou hodnotu, protože ta byla opravdu předaná přes `--model`:

- `claude_code.py:_reported_model()` (řádky 164-184): `model`/`model_id`/`modelId` z
  `raw`/`usage`, jinak spojí jména z `raw["modelUsage"]` (mapa modelů použitých v dané
  session) - **u Claude Code se nikdy nepoužije nakonfigurovaná hodnota jako náhrada**,
  protože Claude Code smí zvolit model sám, i když `--model` není zadán; chybějící
  evidence zůstává `None` (komentář `claude_code.py:170-172`: "Never substitute a
  configured catalog value here... missing provider evidence must remain visibly
  unknown").
- `antigravity.py:_reported_model()` (řádky 138-150): `model`/`model_id`/`modelId` z
  `raw`/`usage`, jinak fallback na `configured_model` (bezpečné, protože bylo skutečně
  předané CLI přes `--model`).
- `codex.py:_reported_model()` (řádky 177-191): projde Codex events, hledá
  `model`/`model_id`/`modelId` uvnitř `usage`/`model_usage`, jinak fallback na
  `configured_model`.
- `gemini.py:_reported_model()` (řádky 122-133): `model`/`model_id`/`modelId` z
  `raw`/`raw["stats"]`, jinak fallback na `configured_model`.

### 5.2 Napříč voláními/providery (`usage_events`, failover, outbox)

- `FailoverAgent.run()` (`failover.py:416-427`) staví pro každé fyzické volání
  providera záznam `{"provider", "source": "reported", "model", "input_tokens",
  "output_tokens", "thinking_tokens", "total_tokens", "cost_usd"}` a připojuje ho do
  `usage_events` - takže i limitovaný/zahozený pokus providera zůstává v evidenci,
  nejen finální úspěšné volání.
- `autonomous.py` sbírá `usage_events` napříč celým autonomním během do
  `AutonomousResult.usage_events` a agreguje je přes `_usage_summary()`
  (`autonomous.py:387-403`) do `usage_by_provider` (součty tokenů/nákladů per
  provider, `source: "reported"`) - **model se v této agregaci ztrácí** (`_usage_summary`
  sčítá jen číselná pole; model je textový a per-provider může být víc hodnot).
- `OrchestratorService._write_autonomous_outbox()` (`service.py:561-658`) zapisuje do
  `outbox/autonomous-<run_id>.json`:
  - `usage.events` (plný seznam, model dostupný per-událost),
  - `usage.by_provider` (bez modelu, jen čísla),
  - `provider_sequence` (unikátní jména providerů v pořadí prvního použití),
  - `active_provider` (poslední použitý provider),
  - **`active_model`** (`service.py:577-586,646`) - zpětně dohledaný jako model
    posledního `usage_events` záznamu, který patří `provider_sequence[-1]` - toto JE
    dnešní nejbližší ekvivalent "vybraný model pro tento výsledek", ale je odvozený
    (poslední reportovaná hodnota aktivního providera), ne explicitně zvolený a
    zaznamenaný v okamžiku volby.
  - **Chybí "reason" pole úplně.** Nic v `AgentRunResult`, `usage_events`,
    `ProviderStatus` ani v outbox payloadu nezaznamenává *proč* byl daný
    provider/model použit (explicitní volba uživatele vs. výchozí `default_agent` vs.
    failover kvůli limitu/nedostupnosti vs. budoucí volba podle typu/složitosti
    úlohy) - jedinou nepřímou indicií je `provider_statuses` (stav KAŽDÉHO providera:
    `NOT_ATTEMPTED`/`AVAILABLE`/`LIMITED`/`PROTOCOL_ERROR`/`BUDGET_EXCEEDED`/
    `TOKEN_BUDGET_EXCEEDED`/`AUDIT_INADEQUATE`/`UNAVAILABLE`), ze kterého lze
    **rekonstruovat**, že např.
    `gemini` byl přeskočen kvůli `LIMITED` a proto skončil aktivní `antigravity` - ale
    to není totéž jako explicitní `reason` pole u výsledného výběru.
  - `outbox/README.md` dokumentuje `provider_sequence`/`provider_statuses`/
    `active_provider`, ale **nezmiňuje `active_model`** - drobná mezera v existující
    dokumentaci opravená touto rešerší (viz komentář v souboru).

### 5.3 Jednorázový (ne-autonomní) `Task`/`run` receipt

`Task` (`models.py`) nese jen `agent` (jméno providera zadané/použité pro úkol) a
`cost_usd` - žádné pole pro model. `outbox/<task_id>.json` (běžný `run`/`import-inbox`
úkol) je plný `Task.to_dict()`, tedy **model se do tohoto receiptu vůbec nedostane** -
jen do `raw_response` uvnitř logu, ne do strukturovaného výstupu. To je asymetrie oproti
autonomnímu běhu (bod 5.2), který `active_model` má.

## 6. Doporučený kontrakt pro předání zvoleného providera, modelu a důvodu

Toto je návrh pro navazující implementační karty (mimo rozsah této rešerše), odvozený
z mezer zjištěných výše. Cíl: AI Project Manager (nebo jiný spotřebitel) musí umět
spolehlivě zjistit (a zobrazit ve Slacku/Trellu) **co bylo vybráno, co bylo skutečně
použito, a proč** - jak pro jednorázový `run`, tak pro `autonomous`.

1. **Vstupní strana - explicitní přání volajícího.** Rozšířit `AgentRunRequest`
   (`agents/base.py`) o volitelné `requested_model: Optional[str] = None` a
   `selection_reason: Optional[str] = None`. Adaptéry, které dnes berou model jen z
   `self.config.model`, by `requested_model` použily jako override (pokud je zadán) a
   jinak spadly na config hodnotu - to umožní AI Project Manageru zvolit model
   per-úloha (podle typu/složitosti) BEZ nutnosti měnit `config.yaml` mezi úkoly.
   `Task` (`models.py`) by získal odpovídající `requested_model`/`selection_reason`
   pole, aby se dala přenést z API/CLI/Inbox vstupu do `AgentRunRequest`.
2. **Výstupní strana - co bylo skutečně použito.** `AgentRunResult.model` už dnes nese
   nejlepší dostupnou evidenci (bod 5.1) - ponechat beze změny sémantiky (nikdy
   nevymýšlet hodnotu, kterou provider nepotvrdil ani nedostal explicitně). Přidat
   `AgentRunResult.model_source: Literal["reported", "configured", "requested"] = ...`
   aby spotřebitel poznal rozdíl mezi "provider to potvrdil sám" a "jen víme, že jsme
   to poslali" - dnes se to řeší jen implicitně uvnitř každého `_reported_model()`.
3. **Sjednotit "receipt" mezi jednorázovým a autonomním tokem.** Doplnit do
   `outbox/<task_id>.json` (běžný `run`) stejnou trojici `active_provider`/
   `active_model`/`selection_reason`, jakou dnes autonomní běh má jen jako
   `active_provider`/`active_model` (bod 5.2) - dnes je to asymetrické (bod 5.3).
4. **Explicitní "reason" pro volbu providera i uvnitř `FailoverAgent`.**
   `ProviderStatus` už rozlišuje DŮVOD, proč byl provider přeskočen
   (`limited`/`protocol_incompatible`/`budget_exceeded`/`audit_inadequate`/
   `unavailable`) - stačí totéž slovníkové zdůvodnění přiřadit k VÝSLEDNÉ volbě
   (aktivnímu provideru), ne jen k odmítnutým: např. `"selection_reason":
   "explicit_agent"` (uživatel zadal `--agent`), `"default_agent"`, nebo
   `"failover: <předchozí_provider> LIMITED"` - poskládat ze stejných dat, která
   `provider_status_snapshot()`/`ProviderStatus` už mají, jen je promítnout do jednoho
   pole namísto nutnosti rekonstruovat z `provider_statuses` mapy.
5. **Historický návrh před v2:** nepřidávat katalog modelů do AI Orchestratoru a
   ponechat jej jako "dumb pipe" pro `requested_model`. Tento návrh byl překonán
   současným v2 kontraktem. Nyní AO odvozuje malý profil konkrétního úkolu,
   broker vlastní katalog a převod profilu na přesné modelové ID; PM workflow
   ani fáze `intake`/`implementation`/`audit` samy model neurčují.

Tyto body 1-4 jsou zpětně kompatibilní (nová volitelná pole, `None`/chybějící hodnota
zachovává dnešní chování) a nemění nic na existujícím failover/audit/finalizace
chování popsaném v `ARCHITECTURE.md`.

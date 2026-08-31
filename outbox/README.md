# outbox/

Sem orchestrátor po dokončení každého úkolu zapíše strojově čitelný výsledek
jako JSON. Nic se odsud nemaže automaticky - jsou to trvalé záznamy pro
dohledatelnost a pro externí nástroj, který výsledky čte (AI Project Manager).

## `<task_id>.json` - běžný úkol (`run`, `import-inbox`)

Celý záznam úkolu z fronty (`Task.to_dict()`): `id`, `status`
(`pending`/`running`/`testing`/`fixing`/`committing`/`waiting_for_provider`/
`done`/`failed`/`error`), `result`, `error`, `test_output`, `tests_passed`,
`committed`, `commit_hash`, `source` (`cli`/`api`/`inbox`) atd.

## `autonomous-<run_id>.json` - autonomní běh (`orchestrator.py autonomous`)

Toto je stabilní handoff kontrakt, který konzumuje AI Project Manager po
tom, co spustí `orchestrator.py autonomous --project <p> --spec <soubor>
--run-id <trello-card-id>` (viz `README.md` kapitola 6 a `--run-id` v
`orchestrator.py autonomous --help`) - `run_id` v souboru vždy odpovídá
`--run-id`, který AI Project Manager zadal, takže výsledek běhu spuštěného
pro konkrétní Trello kartu je vždy dohledatelný přes
`outbox/autonomous-<id_karty>.json`.

Pole, na která je bezpečné se spolehnout (nemění se bez aktualizace tohoto
souboru):

- `run_id` (str) - stejné jako `--run-id` na vstupu.
- `done` (bool) - `true` právě tehdy, když `status == "completed"`
  (Definition of Done splněná a testy prošly). Toto je pole, podle kterého
  AI Project Manager pozná, že má kartu na Trellu posunout/uzavřít.
- `status` (str) - jedno z `running` (nemělo by se objevit v hotovém
  souboru), `completed`, `waiting_for_provider`, `blocked`,
  `max_iterations`, `protocol_error`, `budget_exceeded`, `error`.
- `stop_reason` (str|null) - proč běh skončil, když `done` není `true`:
  chybová hláška agenta, nebo `status` hodnota (`blocked`,
  `max_iterations`, `waiting_for_provider`, `protocol_error`,
  `budget_exceeded`) když žádná konkrétní chyba není k dispozici.
  `budget_exceeded` znamená, že aktivní provider překročil svůj
  nakonfigurovaný `max_budget_usd` (viz `config.example.yaml` -
  `claude_code`/`antigravity`/`codex`) pro tento konkrétní běh a žádný
  další nakonfigurovaný provider nebyl k dispozici k failoveru.
- `limit_hit` (str|null) - nastaveno pouze při
  `status == "waiting_for_provider"`: popis toho, že všichni nakonfigurovaní
  provideři (viz `provider_sequence`) jsou vyčerpaní/nedostupní. Jestli
  orchestrátor tento čekající běh později automaticky obnoví sám, nebo je
  nutné znovu spustit `orchestrator.py autonomous ... --run-id <id>`
  (typický případ - viz kapitola 9), řekne pole `auto_resume_active` níže;
  bez ověření tohoto pole si NIKDY nepředpokládej, že se běh obnoví sám
  (viz produkční incident cb501524e47e, 26.8.2026 - AI Project Manager
  tehdy tuto informaci neměl a úloha zůstala nesprávně zobrazená jako
  probíhající).
- `retry_after_seconds` (float|null) - jen u `waiting_for_provider`: za
  kolik sekund orchestrátor sám zkusí pokračovat.
- `auto_resume_active` (bool|null) - jen u `waiting_for_provider`: `true`
  právě tehdy, když TENTO proces běžel jako trvalý worker/scheduler (viz
  `OrchestratorService(persistent=True)`, používá jen `orchestrator.py
  api`) a jeho vlastní waiting worker po vypršení `retry_after_seconds`
  čekající běh sám obnoví. `false` (běžný případ - `orchestrator.py
  autonomous` je jednorázový proces, viz README kap. 9) znamená, že žádné
  automatické pokračování neběží a AI Project Manager musí po
  `retry_after_seconds` spustit stejný příkaz (se stejným `--run-id`)
  znovu - naváže z checkpointu, ne od bodu 0. `null` mimo
  `waiting_for_provider`, kde otázka nedává smysl.
- `next_step` (str) - text prvního nesplněného bodu Definition of Done
  (prázdný řetězec, pokud jsou splněné všechny).
- `last_output` (str) - poslední textová odpověď agenta (pro rychlý náhled
  bez nutnosti otevírat `iterations`).
- `checkpoint.run_id` (str) - `run_id` běhu, který checkpoint zapsal
  (viz `data/autonomous_checkpoints/`).
- `checkpoint.completed_dod_indices` (list[int]) - indexy bodů Definition
  of Done, které jsou v tomto výsledku splněné.
- `provider_sequence` (list[str]) - unikátní jména providerů (`claude-code`,
  `antigravity`, `codex`), ve kterém byli v tomto běhu skutečně použiti (v
  pořadí prvního použití) - ukazuje, jestli/kam proběhl fallback.
- `active_provider` (str|null) - poslední použitý provider z
  `provider_sequence` (ten, který běh buď dokončil, nebo na kterém čeká).
- `dod_items` (list) - kompletní Definition of Done se stavem. Každý záznam
  obsahuje `text`, `done`, `live_verification` (`command` + `expect`, nebo
  `null`) a `live_evidence` (odvozené `passed`, `exit_code`, skutečný `output`,
  nebo `null`). AI Project Manager tento důkaz zapíše do Trella; u označeného
  integračního bodu nesmí samotné `done` bez `live_evidence.passed=true`
  považovat za dokončení.
- `committed` (bool), `commit_hash` (str|null).
- `error` (str|null) - syrová chybová hláška (stejný zdroj jako
  `stop_reason`, když `stop_reason` chybu jen přebírá).
- `protocol_error_total` (int) - kolik iterací tohoto běhu skončilo
  nevyřešenou protokolovou chybou (agent nevrátil platný JSON kontrakt ani
  po jednom levném repair pokusu - viz `PROTOCOL_ERROR_STREAK_LIMIT`
  v `autonomous.py`). Nenulové i u běhu, který nakonec skončil jinak než
  `status == "protocol_error"` (např. failover na jiného providera stav
  vyřešil) - ukazuje promarněnou spotřebu, ne jen finální stav.
- `protocol_error_wasted_prompt_tokens_estimate` (int) - hrubý odhad
  promarněných tokenů (počet znaků promptů z protokolově chybných iterací
  vydělený 4) způsobených protokolovou chybou; levný náhradník za skutečnou
  spotřebu, když provider `usage` metadata nevrátí (viz `usage.note` níže).
- `breaker_saved_attempts` (int), `restored_from_checkpoint` (int) -
  diagnostické počítadla, ne součást kontraktu pro rozhodování.
- `iterations` (list) - plný log jednotlivých iterací, pro debugging; AI
  Project Manager by na tomto poli neměl stavět rozhodovací logiku, jen ho
  případně přiložit k Trello kartě pro člověka.

Viz `tests/test_cli.py` a `tests/test_service.py` pro ověřené příklady
tohoto kontraktu (úspěšný běh, `waiting_for_provider`, run-id round-trip
přes skutečné CLI).

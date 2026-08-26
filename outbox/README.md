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
  `max_iterations`, `error`.
- `stop_reason` (str|null) - proč běh skončil, když `done` není `true`:
  chybová hláška agenta, nebo `status` hodnota (`blocked`,
  `max_iterations`, `waiting_for_provider`) když žádná konkrétní chyba
  není k dispozici.
- `limit_hit` (str|null) - nastaveno pouze při
  `status == "waiting_for_provider"`: popis toho, že všichni nakonfigurovaní
  provideři (viz `provider_sequence`) jsou vyčerpaní/nedostupní. Orchestrátor
  sám čekající běh později automaticky obnoví (viz `retry_after_seconds`) -
  AI Project Manager nemusí nic spouštět znovu, jen může tuto informaci
  zapsat na kartu.
- `retry_after_seconds` (float|null) - jen u `waiting_for_provider`: za
  kolik sekund orchestrátor sám zkusí pokračovat.
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
- `dod_items` (list[{text, done}]) - kompletní Definition of Done se stavem.
- `committed` (bool), `commit_hash` (str|null).
- `error` (str|null) - syrová chybová hláška (stejný zdroj jako
  `stop_reason`, když `stop_reason` chybu jen přebírá).
- `breaker_saved_attempts` (int), `restored_from_checkpoint` (int) -
  diagnostické počítadla, ne součást kontraktu pro rozhodování.
- `iterations` (list) - plný log jednotlivých iterací, pro debugging; AI
  Project Manager by na tomto poli neměl stavět rozhodovací logiku, jen ho
  případně přiložit k Trello kartě pro člověka.

Viz `tests/test_cli.py` a `tests/test_service.py` pro ověřené příklady
tohoto kontraktu (úspěšný běh, `waiting_for_provider`, run-id round-trip
přes skutečné CLI).

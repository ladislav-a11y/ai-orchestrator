# Autoritativní podklad providerů a modelů pro AI Project Manager

Ověřeno: 2026-09-06

Tento dokument je provozní podklad pro výběr přes již implementovaná pole
`requested_model` a `selection_reason`. Nejde o trvalý seznam názvů modelů: nabídka
závisí na verzi lokálního CLI, přihlášeném účtu, regionu, kvótě a čase. Model je pro
daný běh **ověřeně dostupný** jen tehdy, když jej autoritativní zdroj konkrétního
providera nabízí pro tento účet, nebo když jej provider v úspěšném receipt sám
potvrdí (`model_source: "reported"`).

## Katalog autoritativních zdrojů

| Provider | Explicitní volba | Autoritativní zdroj skutečné nabídky | Co lze bezpečně tvrdit bez ověření |
|---|---|---|---|
| `antigravity` | `requested_model` -> `agy --model` | `agy models` spuštěné stejnou instalací a účtem jako následný běh | Jen to, že adaptér umí model předat; žádný konkrétní slug |
| `claude-code` | `requested_model` -> `claude --model` | Úspěšný běh a providerem reportovaný model; dokumentace CLI připouští alias `sonnet`, `opus` nebo plné jméno | Alias/plné jméno je platný tvar vstupu, nikoli důkaz oprávnění účtu |
| `codex` | `requested_model` -> `codex exec --model` | Úspěšný běh a providerem reportovaný model; nabídka zobrazená přihlášeným Codex klientem je jen kandidát do té doby, než ji běh potvrdí | Jen to, že CLI přijímá explicitní model |
| `gemini` | `requested_model` -> `gemini --model` | Úspěšný běh a providerem reportovaný model pro stejné přihlášení/API klíč | Nakonfigurované `gemini-2.5-flash` je požadavek, ne důkaz dostupnosti |

Antigravity je jediný z integrovaných CLI providerů s explicitním neinvazivním
výpisem nabídky (`agy models`) popsaným výrobcem. Výpis se nesmí přenášet mezi účty
ani uchovávat jako neomezeně platný katalog. U ostatních providerů dokumentace nebo
obecný webový katalog popisuje možné modely, ale neprokazuje dostupnost v konkrétním
lokálním klientu. AI Project Manager proto nesmí odvozovat model z marketingového
seznamu ani hádat slug.

## Omezení implementovaného rozhraní

- `requested_model` je per-request override a má přednost před `<provider>.model`.
  Prázdná hodnota override není explicitní volba; použije se konfigurace nebo default
  samotného CLI.
- AI Orchestrator záměrně nemá statický allowlist názvů. Hodnotu předá beze změny a
  validaci provede provider. Neznámý/nepovolený model musí skončit jako neúspěch;
  nesmí být tiše nahrazen jiným modelem.
- `selection_reason` je vysvětlení volajícího, ne důkaz dostupnosti ani potvrzení
  použitého modelu.
- Automatický `FailoverAgent` přepíná provider při nedostupnosti, limitu, timeoutu,
  opakované chybě protokolu, překročení rozpočtu nebo nedostatečném auditu. Nepřekládá
  modelový slug jednoho providera na jiný. Při failoveru tedy explicitní model může
  být pro dalšího providera neplatný; takový běh musí selhat viditelně.
- Claude Code je přísnější: pokud provider model sám nepotvrdí, receipt ponechá
  `model` i `model_source` neznámé. Ostatní adaptéry mohou uvést `requested` nebo
  `configured`, což potvrzuje pouze hodnotu předanou CLI, nikoli providerové
  potvrzení skutečného provedení.

## Autoritativní provider receipt

AI Project Manager má číst strukturovaný receipt, nikoli log nebo text odpovědi:

- jednorázový běh: `agent`, `requested_model`, `model`, `model_source` a
  `selection_reason` v `outbox/<task_id>.json`;
- autonomní běh: `active_provider`, `active_model`, `selection_reason`,
  `provider_sequence`, `provider_statuses` a `usage.events` v
  `outbox/autonomous-<run_id>.json`.

Síla evidence sestupně:

1. `model_source: "reported"` — provider model sám uvedl; jde o autoritativní receipt.
2. `model_source: "requested"` — orchestrátor model explicitně poslal, provider jej
   ale v odpovědi nepotvrdil.
3. `model_source: "configured"` — orchestrátor poslal statickou konfiguraci, provider
   ji nepotvrdil.
4. `null` — model je neznámý; nesmí se doplnit odhadem.

`usage.events` je autorita pro jednotlivá fyzická volání při failoveru. Souhrn
`usage.by_provider` agreguje spotřebu a není katalogem modelů. `active_model` je
nejlepší dostupná evidence posledního aktivního providera, nikoli sám o sobě záruka
stupně `reported`; ten je třeba číst z příslušné události/receiptu.

## Bezpečný postup AI Project Manageru

1. Chce-li APM vynutit konkrétní model, použije přesný slug z čerstvého
   autoritativního zdroje pro tentýž účet a vyplní `requested_model` i věcný
   `selection_reason`.
2. Není-li nabídka ověřená, APM konkrétní model nevybere. Použije známého providera
   bez override a výsledek označí jako neověřený, dokud receipt nevrátí `reported`.
3. Odmítnutí modelu, chybějící potvrzení nebo rozdíl mezi požadovaným a reportovaným
   modelem se nesmí přepsat na úspěšně ověřené směrování. APM uchová důvod a zvolí
   další model/provider jen v nové, explicitní plánovací volbě.
4. Pro automatický failover APM předává model pouze tehdy, když je stejný slug
   ověřený pro všechny providery v omezeném `provider_order`; jinak má směrovat na
   jednoho explicitního providera nebo model override vynechat.
5. Dočasný výpis nabídky slouží jen k rozhodnutí v daném ticku. Nezapisuje se jako
   dlouhodobý runtime stav a po použití se odstraní; Trello a provider receipt zůstávají
   autoritou workflow a výsledku.

## Ověřené zdroje rozhraní

- Anthropic Claude Code CLI reference: https://docs.anthropic.com/en/docs/claude-code/cli-usage
- Google Antigravity headless CLI: https://www.agy.dev/docs/cli/headless/
- OpenAI model documentation: https://developers.openai.com/api/docs/models/all

Obecný OpenAI katalog je záměrně pouze zdroj možných názvů, ne důkaz dostupnosti v
Codex CLI konkrétního účtu. Stejné omezení platí pro obecné modelové stránky ostatních
výrobců.

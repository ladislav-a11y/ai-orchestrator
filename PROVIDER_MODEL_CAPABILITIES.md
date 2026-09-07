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
4. Pro automatický failover APM předává volitelnou mapu `provider -> model`.
   Každý adapter dostane pouze slug patřící právě jeho providerovi; chybějící
   položka znamená použití providerova vlastního nakonfigurovaného/výchozího
   modelu. Jeden slug se nesmí mechanicky kopírovat mezi různé providery.
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

## Ověřený lokální snapshot a směrovací úrovně

Snapshot níže vznikl 2026-09-06 přímo v runtime prostředí orchestrátoru. Je omezený
na právě přihlášený účet a instalované CLI; není to trvalý katalog. Ověření nepoužilo
žádný modelový požadavek a nevytvořilo souborový artefakt.

| Provider | Ověřená instalace | Autoritativní účetní výsledek | Bezpečný závěr pro routing |
|---|---|---|---|
| `antigravity` | `agy 1.1.27` | `agy models` úspěšně vrátil níže uvedené slugy | Lze použít uvedené přiřazení tierů, dokud nový výpis pro stejný účet slug stále nabízí |
| `claude-code` | CLI v tomto prostředí nenalezeno | Bez úspěšného provider receipt není ověřen žádný slug | Žádný tier nesmí dostat explicitní model; provider je lokálně nedostupný |
| `codex` | `codex-cli 0.149.0`; `codex --help` potvrzuje `-m, --model <MODEL>` | CLI nemá neinvazivní účetní výpis; živý modelový smoke test je podle `AGENTS.md` pouze ruční krok | Žádný tier nemá lokálně ověřený explicitní slug; použít bez override, nebo až slug potvrzený úspěšným receipt |
| `gemini` | `gemini 0.56.0`; upstream CLI reference potvrzuje `--model` | CLI neposkytlo neinvazivní účetní katalog; nakonfigurovaný `gemini-2.5-flash` nebyl modelovým během ověřen | Konfigurace zůstává požadavkem, nikoli ověřenou tier volbou; odmítnutí musí být viditelné a umožnit failover |

Autoritativní výstup `agy models` pro tento snapshot:

| Úroveň | Ověřené provider-specific identifikátory | Použití |
|---|---|---|
| `economical` | `gemini-3.8-flash-low`, `gemini-3.7-flash-low`, `gemini-3.6-flash-low` | Nejnižší deklarovaná effort varianta pro jednoduché, dobře ohraničené úlohy |
| `balanced` | `gemini-3.8-flash-medium`, `gemini-3.7-flash-medium`, `gemini-3.6-flash-medium`, `gpt-oss-120b-medium` | Výchozí kompromis rychlosti a reasoning effort; preferovat nejnovější stále nabízený Flash `medium` |
| `quality` | `gemini-3.8-flash-high`, `gemini-3.7-flash-high`, `gemini-3.6-flash-high`, `gemini-3.1-pro-high`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking` | Složitá implementace nebo audit; konkrétní volbu musí stále potvrdit čerstvý `agy models` |
| bez automatického tieru | `gemini-3.1-pro-low` | Provider potvrzuje dostupnost, ale název kombinuje rodinu Pro s nízkým effort; bez samostatné politiky jej nelze poctivě zařadit |

Přiřazení je lokální routingová politika podle providerem deklarované rodiny a effort
(`low`/`medium`/`high`/`thinking`), ne tvrzení o ceně ani benchmarku. Pořadí slugů v
jedné úrovni není žebříček kvality. Pro `claude-code`, `codex` a `gemini` je prázdná
ověřená množina záměrný výsledek rešerše, nikoli chybějící údaj: jejich současné CLI
nedává bezpečný neinvazivní důkaz nabídky pro tento účet.

## Fail-closed rozhodovací pravidla

- Před explicitní volbou Antigravity obnovit `agy models`; pokud příkaz selže, je
  prázdný nebo vybraný slug chybí, nepředávat `requested_model` a zaznamenat důvod.
- U ostatních providerů přijmout konkrétní slug jen z úspěšného receipt pro stejný
  účet a aktuální CLI. Dokumentace výrobce dokládá syntaxi nebo kandidáta, ne lokální
  oprávnění.
- Chybějící tier, prázdná hodnota či neplatný slug nikdy nenahrazovat domnělým
  provider-specific ekvivalentem. Explicitní single-provider běh skončí chybou;
  automatický běh smí pokračovat pouze běžným failover kontraktem.
- `requested_model` je per-provider hodnota. Při automatickém failoveru ji musí
  volající odvodit z provider-specific mapy; nikdy se nesmí použít jako jediný
  globální slug pro více různých providerů. Tier je záměr, nikoli přenositelný
  název modelu.
- Úspěšný proces bez `model_source: "reported"` nepotvrzuje skutečně použitý model.
  Rozdíl requested/reported je chyba důkazu a nesmí se tiše označit jako splněný tier.

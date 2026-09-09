# Provider Broker a jazyky providerů

Tento soubor popisuje aktuální čistý základ vrstvy providerů. Je určený pro
lidské čtení při údržbě a dalším budování. Strojová data zůstávají v brokerově
poznámkách `data/provider-info/*info.json` a v návodech `orchestrator/agents/lang*.json`.

## Broker

Broker pouze vybere providera pro AO. Nepřijímá pracovní úkol a nespouští
pracovní `run()` žádného providera.

### Pevné pořadí výběru

Broker prochází provideři vždy v tomto pořadí:

1. `groq`
2. `antigravity`
3. `claude-code`
4. `codex`

Vybere prvního providera se stavem `AVAILABLE`. Pokud není dostupný žádný,
vrátí `NONE_AVAILABLE`.

### Brokerovy poznámky při výběru

Broker vede pro každého aktuálního providera samostatnou strojovou poznámku:

`data/provider-info/<provider>info.json`

Při `select_provider` broker nejprve načte a strukturálně ověří poznámku. Pokud
poznámka chybí nebo má stav `UNKNOWN` či `ERROR`, osloví pouze příslušného
providera jeho stavovým `probe_identity()`/dostupnostním dotazem, výsledek uloží
a pokračuje ve výběru. Pracovní `run()` se touto kontrolou nespouští.

Poznámka je proto jediný přenosný zdroj pro rozhodnutí brokeru: obsahuje stav,
skutečně zjištěný nebo nakonfigurovaný model, důvod, úplnou odpověď, usage,
čas kontroly, případný čas dalšího pokusu a poslední známý katalog modelů.
AO z ní dostane nabídku providera s modelem a důvodem, nikoli domněnku.

### Příkazy brokeru

#### `select_provider`

Běžný dotaz AO. Broker načte nebo ověří poznámky podle potřeby a vrátí nabídku
s těmito údaji:

- `provider` — vybraný provider;
- `model` — model, který má AO předat providerovi;
- `model_source` — odkud model pochází, například `reported`,
  `reported_receipt`, `configured` nebo `forced`;
- `selection_mode` — `AUTO` nebo `FORCED`;
- `state` — stav providera;
- `reason` — důvod stavu;
- `info_file` — cesta k brokerově poznámce.

`selection_mode: FORCED` je technický název existujícího brokerového režimu.
V uživatelském a providerovém popisu znamená `fixed_model_selection`, tedy
zafixovaný konkrétní model. Samotný tento stav nepotvrzuje, že provider model
skutečně použil; potvrzení se zjišťuje odděleně z odpovědi nebo metadat providera.

Požadavek na konkrétního providera bez modelu se zadává pouze jeho ID, například
`{"command":"select_provider","provider":"codex"}`. Broker načte aktuální
model z jeho poznámky a vrátí AO stejný návod z příslušného `lang*.json`. AO
model ani komunikační postup nedoplňuje podle vlastního odhadu.

#### `refresh_provider_notes`

Řízený příkaz pro obnovení poznámek všech čtyř providerů. Broker:

1. ověří aktuální stav každého providera;
2. uloží celou odpověď kontroly a dostupnost;
3. požádá providera o katalog modelů, pokud to jeho jazyk umožňuje;
4. uloží aktuální katalog do jeho `*info.json`;
5. porovná nový katalog s posledním potvrzeným katalogem;
6. ohlásí přidané, odebrané nebo změněné modely.

Pokud provider katalog nepotvrdí, stav katalogu je `UNKNOWN` nebo `ERROR` a
starý katalog se nepovažuje za odstraněný. Stejné pravidlo platí pro prázdný
nebo strukturálně neplatný výsledek označený jako `REPORTED`: poslední známé
modely zůstanou v poznámce a výsledek se uloží jako neporovnatelný. Refresh,
běžný AO dotaz i další zápis poznámky tak katalog modelů nemaže.

#### `set_provider_model`

Příkaz AO nebo ruční příkaz pro změnu modelu v brokerově poznámce. Modely se
nezapisují do `lang*.json` ani do obecného konfiguračního aliasu.

Příklad nucení přesného modelu Groq:

```json
{
  "command": "set_provider_model",
  "provider": "groq",
  "model_id": "openai/gpt-oss-120b",
  "source": "user",
  "mode": "FORCED"
}
```

Režimy:

- `FORCED` — broker uloží přesný `model_id` do `groqinfo.json` a při nabídce ho
  vrátí AO;
- `AUTO` — nucený výběr zruší a broker znovu používá aktuálně zjištěný model
  providera.

Je-li katalog providera známý, `FORCED` přijme pouze přesné ID z posledního
potvrzeného katalogu. Při neznámém katalogu broker model nehádá, ale explicitně
zadaný model uloží jako požadovaný nucený výběr.

Jakmile je v poznámce `selection_mode: "FORCED"`, může `selected_model` změnit
nebo zrušit pouze explicitní uživatelský příkaz se `source: "user"`. AO,
automatický refresh ani jiný ne-uživatelský zdroj takovou změnu odmítne a
poznámka zůstane beze změny.

### Obsah brokerovy poznámky `*info.json`

Každý provider má vlastní soubor v `data/provider-info/`:

| Pole | Význam |
|---|---|
| `provider` | Název providera. |
| `model` | Model naposledy zjištěný nebo nakonfigurovaný providerem. |
| `model_source` | Zdroj hodnoty modelu, například `reported`, `reported_receipt` nebo `configured`. |
| `state` | `AVAILABLE`, `UNAVAILABLE`, `UNKNOWN` nebo `ERROR`. |
| `checked_at` | Čas poslední kontroly. |
| `available_at` | Čas, od kterého je provider dostupný, pokud je známý. |
| `retry_at` | Čas dalšího pokusu nebo obnovení limitu, pokud je známý. |
| `reason` | Lidsky čitelný důvod aktuálního stavu. |
| `full_response` | Celá odpověď nebo chyba od providera. |
| `response_kind` | Druh uložené odpovědi. |
| `probe_kind` | Způsob provedené kontroly. |
| `usage` | Dostupná informace o využití. |
| `known_responses` | Počty odpovědí, kterým broker rozumí. |
| `unknown_responses` | Odpovědi, které broker zatím neumí zařadit. |
| `model_catalog` | Poslední katalog modelů a jeho stav. |
| `model_update` | Výsledek porovnání katalogu s předchozím katalogem. |
| `selected_model` | Přesný model uložený pro nucený výběr; jinak `null`. |
| `selection_mode` | `AUTO` nebo `FORCED`. |
| `selection_source` | Kdo volbu nastavil, například `ao` nebo `manual`. |
| `selection_updated_at` | Čas poslední změny volby modelu. |

`LIMITED` není samostatná hodnota pole `state`. Limit se v poznámce zachytí
jako `UNAVAILABLE` spolu s důvodem v `reason`, celou zdrojovou odpovědí v
`full_response` a případným absolutním časem v `retry_at`. Pokud provider čas
obnovení neposkytne, zůstává `retry_at: null` a broker nesmí datum ani čas
domýšlet.

Aktuální počet a přesná ID modelů se neudržují v tomto guide. Broker je získává
providerovým katalogovým dotazem, ukládá do `model_catalog` a při refreshi
porovnává v `model_update`. Proto se po přidání nebo odebrání modelu aktualizuje
poznámka; guide se mění jen při změně schopnosti nebo datového kontraktu
brokeru.

### Stav katalogu modelů

- `REPORTED` — provider vrátil aktuální katalog;
- `UNKNOWN` — katalog nelze z dostupného rozhraní zjistit;
- `ERROR` — pokus o zjištění katalogu skončil chybou.

Porovnání katalogů používá výsledky `BASELINE_CREATED`, `UNCHANGED`,
`UPDATED` a `NOT_COMPARABLE`. Katalog je uložený u brokera, ne v jazyce
providera.

## Providers

Každý provider má vlastní adapter `.py` a vlastní návod/překladač `lang*.json`:

| Provider | Adapter | Jazyk |
|---|---|---|
| Groq | `orchestrator/agents/groq.py` | `orchestrator/agents/langgroq.json` |
| Antigravity | `orchestrator/agents/antigravity.py` | `orchestrator/agents/langantigravity.json` |
| Claude Code | `orchestrator/agents/claude_code.py` | `orchestrator/agents/langclaude-code.json` |
| Codex | `orchestrator/agents/codex.py` | `orchestrator/agents/langcodex.json` |

Adapter obsahuje prováděcí logiku. `lang*.json` obsahuje popis, jak s daným
providerem mluvit. Když se změní rozhraní providera, nejprve se aktualizuje
jeho jazyk a teprve potom odpovídající adapter.

Každý provider má vedle svého `lang*.json` také dva vlastní JSON soubory spotřeby:

- `usage_<provider>.json` — pouze poslední běh/úloha providera;
- `usage_<provider>_lifetime.json` — kumulace od prvního záznamu.

Oba soubory mají spotřebu rozdělenou pod přesným úplným ID skutečně použitého
modelu v `models`. U každého modelu jsou pouze `input_tokens`, `output_tokens`,
`thinking_tokens`, `total_tokens` a `cost_usd`. Číselná hodnota se zachová;
`null` se zapisuje jako `0`. Evidence nepoužívá požadovaný nebo zkrácený název
modelu jako náhradu skutečně reportovaného modelu.

### Providerová notifikace do Slacku

Každý provider po dokončení svého běhu zapíše usage a ještě před návratem z
`run()` odešle stručnou zprávu do kanálu `#ai-status`. Odeslání je součástí
providerového adaptéru, broker se ho neúčastní. Zpráva obsahuje datum a čas,
název providera, konkrétní `úkol` převzatý z původního AO promptu, samostatný
`stav` (`completed`, `failed`, `limited` nebo `unavailable`), přesné úplné ID
použitého LLM, `input_tokens`, `output_tokens`, `thinking_tokens`,
`total_tokens` a `cost_usd`.

Notifikace používá stejné hodnoty, které provider předal usage ledgeru, a usage
JSON znovu nečte. `null` nebo chybějící hodnota se zobrazí jako `0`. Slack je
best-effort observability: chyba odeslání nesmí změnit výsledek úlohy. Úspěch
se potvrzuje pouze JSON odpovědí Slack API s `ok: true`, nikoli samotným HTTP
status kódem. Přístupový token se načítá z lokálního neveřejného souboru
`config/slack_bot_token.txt`; do repozitáře ani do environment proměnných se
neukládá.

Identita LLM se pro Slack bere výhradně z providerem potvrzeného výsledku:
nejdříve z `result.model`, potom z přesného `receipt_model` nebo z odpovědi
providera (`model` či jednoznačný klíč `modelUsage`). `requested_model` se jako
skutečně použitý model nikdy nepoužije. Providerová odpověď může být při
zpracování obalena Markdownovým blokem ` ```json `; tento obal se při čtení
receiptu ignoruje, ale obsah musí stále obsahovat přesné celé ID modelu.

### Co obsahuje každý `lang*.json`

- `contract_version` — verze kontraktu;
- `provider` a `adapter` — jednoznačné přiřazení návodu;
- `purpose` — účel adapteru;
- `identity_probe` — způsob zjištění identity, modelu, limitu a celé odpovědi;
- `identity_probe.model_catalog` — způsob získání katalogu modelů bez ukládání
  seznamu modelů do jazyka;
- `identity_probe.model_selection` — jak adapter předá brokerem vybraný model;
- `task_execution_receipt` — společný způsob, jak si při úkolu vyžádat `answer`
  a úplné skutečně použité `model`;
- `input_from_ao` — společný vstup, který provider přijímá od AO;
- `translation_to_provider` — překlad společného vstupu do API nebo CLI;
- `input_from_provider` — formát odpovědi providera;
- `output_to_ao` — převod úspěchu nebo chyby zpět do společného výsledku AO.

### Společný pracovní výsledek při zafixovaném modelu

Každý `lang*.json` obsahuje `task_execution_receipt`. Receipt instrukce se
nesmí automaticky připojit k pracovní zprávě, pokud provider současně používá
nástroje. Odesílatel předá podle návodu provideru samostatně původní pracovní
prompt, `output_schema` a receipt instrukci. Receipt se vyžádá až v závěrečné
fázi bez nástrojů a výsledkem je jediný JSON objekt:

```json
{"answer":"<výsledek úkolu>","model":"<skutečně použitý úplný model>"}
```

`model` se nesmí opsat z požadavku. Konkrétní lang určuje, zda je textový
receipt autoritativní, nebo pouze diagnostický; pokud provider poskytne
strojová metadata, mají přednost. Pro Codex je při chybějících metadatech
přesný model z povinného receipt platným doložením identity.
Zafixovaný model je požadavek na výběr, ne podmínka přijetí výsledku. Když není
dostupný nebo je přemapován, úkol se při
použitelné odpovědi dokončí dostupným modelem a zaznamená se skutečně doložené
ID nebo stav `UNVERIFIED`. Rozdíl mezi požadovaným a skutečně doloženým modelem
je `MISMATCH`, který se pouze uloží spolu s oběma ID a nevyvolává odmítnutí,
failover ani jinou automatickou akci.

### Aktuální možnosti jazyků

#### Groq — `langgroq.json`

- katalog: API metadata přes `client.models.list()`;
- práce s modelem: přes API pole `model`;
- výběr: přesné `model_id`, bez aliasů;
- identita: lokální konfigurace nevyžaduje inference požadavek;
- při limitu se zachovává celá chyba a údaje o kvótě.
- pracovní fáze používá původní prompt a povolené nástroje;
- při zafixovaném modelu se `task_execution_receipt` provádí až v tool-free finální fázi
  přes `AgentRunRequest.output_schema` a `AgentRunRequest.receipt_prompt`;
- pracovní výsledek je jediný JSON objekt s poli `answer` a `model`; při
  nedostupnosti zafixovaného modelu stačí skutečné úplné ID dostupného modelu.

#### Antigravity — `langantigravity.json`

- katalog: read-only příkaz `agy models`;
- práce s modelem: CLI argument `--model`;
- výběr: přesné `model_id`, bez aliasů;
- identita: neinteraktivní JSON probe;
- při limitu se zachovává přímá odpověď a informace o resetu.
- pracovní výsledek: jediný JSON objekt s poli `answer` a `model`; při nedostupnosti
  vnuceného modelu stačí skutečné úplné ID dostupného modelu.

#### Claude Code — `langclaude-code.json`

- katalog: broker používá výhradně read-only Anthropic Models API `GET /v1/models`,
  pokud je dostupný `ANTHROPIC_API_KEY`; providerový adapter tento klíč nikdy
  nepoužívá. Bez klíče broker použije CLI picker a přesná metadata z probe;
- `ANTHROPIC_API_KEY` vlastní pouze broker a smí být použit pouze při refreshi
  katalogu modelů. Nesmí se předat do identity probe ani do pracovního běhu;
  Claude Code provider se pro práci vždy autentizuje přihlášením Claude CLI;
- práce s modelem: `--model`, případně `--fallback-model`;
- nucení modelu: broker přijme `set_provider_model` s `provider: claude-code`,
  přesným `model_id` z katalogu `REPORTED`, `source: user` a `mode: FORCED`.
  AO následně předá `selected_model` jako `AgentRunRequest.requested_model` a
  adapter ho použije přesně jako `--model <model_id>`. Alias se při nuceném
  výběru nepoužívá; skutečná identita se stále potvrzuje až z odpovědi providera;
- výběr: přesné providerem doložené ID nebo doložený alias;
- identita: neinteraktivní JSON probe;
- při limitu se zachovává `api_error_status`, celý `result` nebo `error` a
  údaje o limitu.
- pracovní výsledek: jediný JSON objekt s poli `answer` a `model`; při nedostupnosti
  vnuceného modelu stačí skutečné úplné ID dostupného modelu.

#### Codex — `langcodex.json`

- katalog: read-only příkaz `codex debug models`; vrací JSON objekt s položkami
  `models[]`, kde `slug` je přesné ID modelu;
- adapter zachová celý katalog v brokerově `codexinfo.json`, včetně metadat,
  a broker jej porovná s posledním potvrzeným katalogem;
- práce s modelem: CLI argument `--model <model_id>`;
- výběr: přesné providerem doložené `model_id`, bez aliasů; nucený výběr se
  ukládá jako `FORCED` a broker ho předá AO;
- identita: read-only JSONL probe s `--ignore-user-config`;
- ověření identity ukládá skutečně vrácený model a stav `MATCH`, `MISMATCH`
  nebo `UNVERIFIED`. Probe je diagnostický: `MISMATCH` znamená pouze, že
  provider vrátil jinou identitu, než bylo požadováno, nikoli že probe nebo
  provider selhal. Probe musí vyžadovat jediný plain-text řádek s úplným
  kanonickým ID včetně suffixu nebo jiné specializace, zakázat obecné `GPT-5`
  a zakázat opsání požadovaného ID. Například při vnucení `gpt-5.6-luna` a
  odpovědi `gpt-5-codex` nebo `gpt-5.3-codex` se uloží skutečná odpověď jako
  užitečný důkaz konkrétní varianty Codexu; `MISMATCH` je v tomto případě
  očekávané diagnostické porovnání. Požadovaný model se nikdy nepovažuje za
  potvrzený jen proto, že byl předán v `--model`;
- pracovní úkol: volající předá `task_execution_receipt` z `langcodex.json`
  jako samostatné `AgentRunRequest.receipt_prompt` u každého běhu, i když model
  není zafixovaný, a vyžádá jediný JSON objekt s přesně poli `answer` a `model`.
  `requested_model`,
  `metadata_model` a `receipt_model` se evidují odděleně. Za skutečnou identitu
  se považují metadata, nebo u Codexu při jejich absenci přesný receipt model;
  ten se přijme jako `reported_receipt`
  a porovná se jako `MATCH`/`MISMATCH`; `UNVERIFIED` zůstává pouze pro případ,
  kdy chybí metadata i platný receipt model;
- `MISMATCH` se pouze zaznamená spolu s požadovaným a skutečně vráceným ID.
  Nesmí se vyhodnocovat jako chyba ani použít pro odmítnutí úkolu, failover,
  změnu dostupnosti nebo změnu výběru modelu;
- při chybě nebo limitu se zachovají JSONL metadata, chyba a případné údaje o
  limitu. Poslední známý katalog se nemaže.

## Pravidlo aktualizace guide a katalogů

Tento guide popisuje schopnosti, kontrakty a vlastnictví dat; není zdrojem
aktuálního seznamu modelů. Přesné aktuální katalogy a jejich čas kontroly patří
výhradně do `data/provider-info/*info.json`.

Při změně rozhraní providera nebo způsobu zjišťování modelů se postupuje takto:

1. nejprve se upraví příslušný `lang*.json` — příkaz, formát, cesty k modelu,
   limitní chování a pravidla výběru;
2. potom se upraví odpovídající adapter `.py`, který má pouze technickou
   implementaci popsaného kontraktu;
3. provede se řízený refresh katalogů a ověří se uložené `*info.json`;
4. tento guide se aktualizuje pouze tehdy, když se změnila schopnost, kontrakt
   nebo vlastnictví dat — ne při každém přidání či odebrání modelu v katalogu.

Pro Codex je katalogový zdroj vždy `codex debug models`; seznam modelů se do
tohoto guide nekopíruje. Pokud katalog selže, je prázdný nebo není porovnatelný,
broker zachová poslední známý katalog a jeho stav označí jako
`NOT_COMPARABLE`, `UNKNOWN` nebo `ERROR` podle skutečného výsledku.

## Pravidlo vlastnictví dat

- seznam aktuálních modelů a jejich porovnání vlastní broker v `*info.json`;
- návod, jak provider oslovit a jak předat model, vlastní jeho `lang*.json`;
- prováděcí logiku vlastní jeho adapter `.py`;
- AO dostává od brokeru pouze nabídku providera s modelem, stavem a důvodem;
- provider sám pouze provede práci zadanou AO a vrátí úspěch nebo přesný důvod,
  proč ji nesplnil.

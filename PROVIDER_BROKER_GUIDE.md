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

### Příkazy brokeru

#### `select_provider`

Běžný dotaz AO. Broker načte nebo ověří poznámky podle potřeby a vrátí nabídku
s těmito údaji:

- `provider` — vybraný provider;
- `model` — model, který má AO předat providerovi;
- `model_source` — odkud model pochází, například `reported`, `configured` nebo
  `forced`;
- `selection_mode` — `AUTO` nebo `FORCED`;
- `state` — stav providera;
- `reason` — důvod stavu;
- `info_file` — cesta k brokerově poznámce.

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
| `model_source` | Zdroj hodnoty modelu, například `reported` nebo `configured`. |
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

### Co obsahuje každý `lang*.json`

- `contract_version` — verze kontraktu;
- `provider` a `adapter` — jednoznačné přiřazení návodu;
- `purpose` — účel adapteru;
- `identity_probe` — způsob zjištění identity, modelu, limitu a celé odpovědi;
- `identity_probe.model_catalog` — způsob získání katalogu modelů bez ukládání
  seznamu modelů do jazyka;
- `identity_probe.model_selection` — jak adapter předá brokerem vybraný model;
- `input_from_ao` — společný vstup, který provider přijímá od AO;
- `translation_to_provider` — překlad společného vstupu do API nebo CLI;
- `input_from_provider` — formát odpovědi providera;
- `output_to_ao` — převod úspěchu nebo chyby zpět do společného výsledku AO.

### Aktuální možnosti jazyků

#### Groq — `langgroq.json`

- katalog: API metadata přes `client.models.list()`;
- práce s modelem: přes API pole `model`;
- výběr: přesné `model_id`, bez aliasů;
- identita: lokální konfigurace nevyžaduje inference požadavek;
- při limitu se zachovává celá chyba a údaje o kvótě.

#### Antigravity — `langantigravity.json`

- katalog: read-only příkaz `agy models`;
- práce s modelem: CLI argument `--model`;
- výběr: přesné `model_id`, bez aliasů;
- identita: neinteraktivní JSON probe;
- při limitu se zachovává přímá odpověď a informace o resetu.

#### Claude Code — `langclaude-code.json`

- katalog: současné CLI neposkytuje katalogový příkaz, proto je stav `UNKNOWN`;
- práce s modelem: `--model`, případně `--fallback-model`;
- výběr: přesné providerem doložené ID nebo doložený alias;
- identita: neinteraktivní JSON probe;
- při limitu se zachovává `api_error_status`, celý `result` nebo `error` a
  údaje o limitu.

#### Codex — `langcodex.json`

- katalog: současné CLI neposkytuje katalogový příkaz, proto je stav `UNKNOWN`;
- práce s modelem: CLI argument `--model`;
- výběr: přesné providerem doložené `model_id`, bez aliasů;
- identita: read-only JSONL probe s ignorováním uživatelské konfigurace;
- při chybě se zachovají JSONL metadata, chyba a případné údaje o limitu.

## Pravidlo vlastnictví dat

- seznam aktuálních modelů a jejich porovnání vlastní broker v `*info.json`;
- návod, jak provider oslovit a jak předat model, vlastní jeho `lang*.json`;
- prováděcí logiku vlastní jeho adapter `.py`;
- AO dostává od brokeru pouze nabídku providera s modelem, stavem a důvodem;
- provider sám pouze provede práci zadanou AO a vrátí úspěch nebo přesný důvod,
  proč ji nesplnil.

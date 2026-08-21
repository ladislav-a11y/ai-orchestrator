# ai-orchestrator

Lokální AI orchestrátor pro Windows 11. Řídí AI agenty (zatím Claude Code,
později i OpenAI Codex), kteří pracují na tvých projektech - spustí agenta na
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

`auto_commit: false` je výchozí nastavení - orchestrátor tedy zatím NIKDY
sám necommituje, dokud to v `config.yaml` (sekce `git`) ručně nezapneš.

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

Běh skončí jedním ze čtyř stavů:

- **completed** - všechny body Definition of Done splněné A testy prošly (nebo
  žádné testy nejsou nastavené). Pokud je navíc zapnutý `git.auto_commit` v
  `config.yaml`, vytvoří se Git commit. Pokud testy neprošly, commit se
  **nikdy** nevytvoří.
- **blocked** - stejný stav (stejné nesplněné body + stejný výsledek testů)
  se opakuje 3x po sobě bez posunu - orchestrátor to nezkouší dál dokola.
- **max_iterations** - vyčerpán limit iterací, Definition of Done pořád není
  splněná celá.
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

`inbox/` a `outbox/` jsou připravené pro budoucí automatické předávání
úkolů/výsledků mezi orchestrátorem a jiným nástrojem, viz README v každém
z nich. Zatím to lze použít i ručně: `python orchestrator.py import-inbox`.

## Bezpečnostní pravidla (proč se to takhle chová)

- Orchestrátor **nikdy** nepoužije `--dangerously-skip-permissions` ani
  jinou obdobu obcházení kontroly oprávnění - je to natvrdo zakázané i v
  konfiguraci (viz `AGENTS.md`).
- Agent smí pracovat jen uvnitř pracovního prostoru (`workspace_root`,
  výchozí `D:\orchestrator`) - jakýkoliv projekt mimo něj (v `config.yaml`
  i v `--project`) orchestrátor natvrdo odmítne.
- **Nikdy nesmaže Git historii** ani nepoužije force push. Push na internet
  v této fázi vůbec neexistuje.
- Commit vznikne jen tehdy, když to povolíš (`git.auto_commit: true`) A
  zároveň testy prošly (nebo pro daný projekt žádné testy nejsou nastavené).
- API poslouchá jen na `127.0.0.1` - z internetu se k němu nedostaneš.

Další podrobnosti architektury jsou v [ARCHITECTURE.md](ARCHITECTURE.md),
pravidla pro AI agenty pracující v tomto repozitáři jsou v
[AGENTS.md](AGENTS.md).

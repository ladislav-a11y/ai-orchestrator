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
  muj-projekt:
    path: "D:/cesta/k/projektu"
    test_command: "pytest"   # nepovinné - jak spustit testy tohoto projektu
```

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

## 6. Zjištění stavu úkolů

```bash
.venv\Scripts\python orchestrator.py status
.venv\Scripts\python orchestrator.py status <id-ukolu>
```

## 7. Lokální API (zatím jen pro tvůj počítač)

```bash
.venv\Scripts\python orchestrator.py api
```

Spustí HTTP API na `http://127.0.0.1:8765` (jen na tomto počítači, nikam
ven). To je příprava na budoucí propojení s jinými nástroji, např. mostem
z ChatGPT - zatím to nikam nepřipojujeme.

## 8. Adresáře inbox/outbox

`inbox/` a `outbox/` jsou připravené pro budoucí automatické předávání
úkolů/výsledků mezi orchestrátorem a jiným nástrojem, viz README v každém
z nich. Zatím to lze použít i ručně: `python orchestrator.py import-inbox`.

## Bezpečnostní pravidla (proč se to takhle chová)

- Orchestrátor **nikdy** nepoužije `--dangerously-skip-permissions` ani
  jinou obdobu obcházení kontroly oprávnění - je to natvrdo zakázané i v
  konfiguraci (viz `AGENTS.md`).
- **Nikdy nesmaže Git historii** ani nepoužije force push. Push na internet
  v této fázi vůbec neexistuje.
- Commit vznikne jen tehdy, když to povolíš (`git.auto_commit: true`) A
  zároveň testy prošly (nebo pro daný projekt žádné testy nejsou nastavené).
- API poslouchá jen na `127.0.0.1` - z internetu se k němu nedostaneš.

Další podrobnosti architektury jsou v [ARCHITECTURE.md](ARCHITECTURE.md),
pravidla pro AI agenty pracující v tomto repozitáři jsou v
[AGENTS.md](AGENTS.md).

# inbox/

Sem se v budoucnu budou ukládat úkoly z externích zdrojů (např. z mostu na
ChatGPT), aby je nebylo nutné ručně kopírovat do příkazové řádky.

Formát jednoho souboru `neco.json`:

```json
{
  "project": "ai-orchestrator",
  "prompt": "Zadání úkolu pro agenta",
  "test_command": null,
  "auto_commit": null
}
```

Pouze `project` a `prompt` jsou povinné. Spusť `python orchestrator.py
import-inbox` a soubory se zařadí do fronty a přesunou do `inbox/processed/`
(nikdy se nemažou).

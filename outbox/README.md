# outbox/

Sem orchestrátor po dokončení každého úkolu zapíše `<task_id>.json` s celým
záznamem úkolu (stav, výsledek, výstup testů, informace o commitu, chyba).
V budoucnu odsud bude externí most (např. napojení na ChatGPT) číst výsledky.

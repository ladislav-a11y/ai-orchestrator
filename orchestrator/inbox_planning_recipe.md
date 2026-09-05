# Recept pro AI Inbox planner

Tento recept převádí lidské zadání na jednu nebo více karet v seznamu
`Připraveno`. Planner pouze plánuje. Neimplementuje, neupravuje soubory,
nespouští audit a nevydává `accepted` ani `rejected`.

AI intake musí lidské zadání rozložit na logicky navazující atomické úlohy.
Bez ohledu na druh aplikace nesmí:

- smíchat změny ve dvou různých projektech nebo repozitářích;
- spojit rešerši, implementaci základního mechanismu a navazující integraci
  do jednoho commitu, pokud jde o samostatné technické výsledky;
- vytvořit kartu závislou na rozhraní, datech, komponentě nebo aplikaci, která
  ještě neexistuje nebo nebyla ověřena;
- předat auditorovi směs změn z různých projektů;
- rozdělit práci pouze mechanicky podle odstavců zadání, souborů nebo názvů
  vrstev; hranice karet musí odpovídat skutečným technickým hranicím.

## 1. Nejprve pochop zadání a současný stav

- Rozliš, co už je podle vstupu nebo předaného projektového kontextu známé a
  co se musí teprve ověřit.
- `configured_projects` je autoritativní katalog existujících projektových
  identit a checkoutů. Nepovažuj projekt za neznámý jen proto, že zrovna nemá
  aktivní workflow kartu v `existing_projects`.
- Pokud katalog obsahuje `AI Project Manager` a `AI Orchestrator`, respektuj
  jejich známou řídicí hranici: AI Project Manager vlastní Trello workflow,
  klasifikaci a rozhodovací politiku, plánování a Slack/Trello notifikace;
  AI Orchestrator vlastní adaptéry providerů, skutečné volání providerů a
  modelů, provider receipt, controllerovou finalizaci a nezávislý audit.
  Rešerše a změna schopností providerových adaptérů proto patří do AI
  Orchestratoru; klasifikace a použití těchto schopností v plánování patří do
  AI Project Manageru a závisí na existujícím rozhraní orchestrátoru.
- Pokud lidské zadání současně požaduje výběr providera/modelu podle typu či
  složitosti a jeho zobrazení v notifikacích, jsou hranice čtyři: (1) rešerše
  schopností v AI Orchestratoru, (2) providerové rozhraní a receipt v AI
  Orchestratoru, (3) klasifikace a rozhodovací politika v AI Project Manageru,
  (4) Slack/Trello notifikační integrace v AI Project Manageru. Tyto výsledky
  nespojuj; každý přímo závisí na předchozím.
- Nehádej architekturu, rozhraní, podporované modely, vlastnictví kódu ani
  umístění změny.
- Jestli bezpečná implementace závisí na neznámé skutečnosti, vytvoř nejprve
  samostatnou rešeršní kartu. Jejím výsledkem musí být konkrétní podklad pro
  následující změnu: skutečný vlastník, rozhraní, možnosti, omezení a doporučený
  postup. Případné pokusné soubory jsou dočasné a musí být po ověření uklizeny.
- Pokud teprve rešerše určí, který projekt má změnu vlastnit, nehádej předem
  cílový projekt následné implementace. V prvním plánu vytvoř pouze bezpečně
  přiřaditelnou rešeršní práci. Následné implementační karty se musí připravit
  nebo zpřesnit až z jejího skutečného výsledku.
- Pokud jsou vlastníci následných změn z lidského zadání a
  `configured_projects` jednoznační, vrať už v tomto plánu celý závislostní
  řetězec. Neznámá schopnost providera vyžaduje rešeršní předpoklad, ale sama o
  sobě není důvodem odložit vytvoření jasně vlastněné navazující karty; tu
  formuluj jako změnu provedenou podle ověřeného výstupu rešerše.
- Jestli rešeršní karta teprve slibuje určit vlastníka změny, nesmí tentýž plán
  současně předem přiřadit její navazující implementaci. Buď je vlastnictví
  podložené katalogem a výše uvedenými hranicemi už nyní, nebo plán v tomto
  bodě končí rešerší a pokračování se připraví až z jejího výsledku.

## 2. Jedna karta znamená jednu atomickou změnu

Každá karta musí mít právě jeden konkrétní výsledek v jednom cílovém projektu
nebo repozitáři. Tento výsledek musí jít samostatně implementovat, ověřit,
controllerem finalizovat a nezávisle auditovat.

Do stejné karty patří kód, konfigurace, potřebné testovací změny a dokumentace,
pokud společně tvoří jedinou změnu ve stejném projektu. Nerozděluj práci jen
podle souborů nebo interních vrstev.

Samostatnou kartu vytvoř, když platí alespoň jedna z podmínek:

- nejdříve je nutná rešerše nebo ověřovací prototyp;
- změna patří do jiného projektu nebo repozitáře;
- jde o samostatně dokončitelnou a auditovatelnou změnu s vlastním výsledkem;
- pozdější práce nemůže bezpečně začít před dokončením předchozího výsledku.

Každá karta musí ve `scope` a `task` jednoznačně pojmenovat svůj cílový projekt.
Pokud zadání zasahuje více projektů, vytvoř pro každý projekt samostatné karty.
Nespojuj změny ve více repozitářích do jedné karty.
Pole `project_key` musí obsahovat přesně jednu existující identitu z
`configured_projects`. Je autoritativní pro přiřazení cílového repozitáře;
nespoléhej na hádání identity ze slov použitých ve `scope` nebo `task`.

Každá karta musí mít vlastní projektovou identitu, přesný technický rozsah,
testovací změny odpovídající jejímu výsledku a vlastní controllerovou
finalizaci. Auditor musí nad jednou kartou dostat pouze změny tohoto jednoho
projektu a tohoto jednoho výsledku.

## 3. Sestav skutečnou posloupnost

- `depends_on` obsahuje zero-based indexy pouze přímých předpokladů stejného
  Inbox batchu.
- Závislosti musí být acyklické. Priorita nikdy nenahrazuje závislost.
- Rešerše předchází implementaci, která její výsledek potřebuje.
- Producent rozhraní nebo dat předchází jejich spotřebiteli.
- Základ aplikace nebo komponenty předchází funkci, která na něm závisí.
- Následující karta se nesmí formulovat tak, jako by dosud neověřený výsledek
  už existoval. Musí výslovně navázat na výstup své předchozí karty.

## 4. Nevytvářej nesplnitelné implementační úkoly

- Standardní spuštění testů, Git diff, commit, push, remote ověření, live
  evidence, nezávislý audit a verdikt `accepted`/`rejected` jsou odpovědností
  controlleru a auditní fáze. Nevytvářej z nich implementační karty ani je
  nepřidávej jako práci implementačního agenta.
- Implementační karta může požadovat vytvoření nebo opravu testů jako součást
  změny. Samotné spuštění a vyhodnocení testovací sady ale vlastní orchestrátor.
- Úkol nesmí vyžadovat přístup, rozhodnutí nebo ruční zásah, který cílový agent
  nemá. Takovou skutečnost popiš jako předpoklad k ověření v předchozí rešerši,
  nikoli jako údajně splnitelnou implementaci.

## 5. Zkontroluj plán před odesláním

Před vrácením JSON si beze změny výstupního formátu ověř:

1. Má každá karta jediný výsledek a jediný cílový projekt/repozitář?
2. Je každá změna samostatně finalizovatelná a auditovatelná?
3. Předchází rešerše a producenti všem závislým implementacím a spotřebitelům?
4. Neobsahuje žádná karta controllerovou finalizaci nebo nezávislý audit jako
   implementační práci?
5. Jsou všechny přímé závislosti uvedené v `depends_on` a nevzniká cyklus?
6. Vysvětluje `split_reason` věcně, proč karta existuje samostatně?

## 6. Výstup

Vrať pouze JSON odpovídající poskytnutému output schema. `work_type` smí být
jen `implementation`, `research`, `configuration`, `integration` nebo `tests`.
`tests` použij samostatně pouze pro skutečný testovací artefakt či infrastrukturu,
nikoli pro běžné spuštění testů nebo audit. Každá priorita musí být unikátní a
musí mít konkrétní `priority_reason` vycházející ze vstupu.
Potvrzená oprava, regrese, rework nebo změna samotného PM/orchestrátoru má vždy
prioritu P5; desetinné podpriority pouze určují pořadí sourozenců v této úrovni.

Příklad typu rozkladu, nikoli povinná šablona: nejprve ověřit neznámé možnosti,
rozhraní a vlastnictví; potom změnit komponentu, která potřebná data nebo
rozhraní poskytuje; následně změnit každý závislý projekt, který je spotřebuje;
nakonec doplnit uživatelskou integraci pouze tehdy, pokud jde o samostatně
finalizovatelnou změnu. Může jít o libovolnou aplikaci nebo kombinaci aplikací.
Každý krok je samostatná karta jen tehdy, když odpovídá výše uvedeným pravidlům,
a každý pozdější krok přímo závisí na výsledku, který potřebuje.

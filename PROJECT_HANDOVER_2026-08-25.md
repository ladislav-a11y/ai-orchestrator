\# PROJECT HANDOVER — 2026-08-25



\## Účel dokumentu



Tento dokument je stavový checkpoint pro pokračování práce na projektech:

\- AI Project Manager

\- AI Orchestrator

\- AI Station Agent



Slouží jako jediný přenosový bod při přechodu mezi chaty. Nové vlákno má pokračovat od tohoto stavu a neopakovat již vyřešené problémy.



\---



\# 1. AI Project Manager



\## Účel



AI Project Manager je řídicí vrstva pro správu AI projektů.



Hlavní princip:

\- Trello = jediný zdroj pravdy pro stav projektů

\- Git = zdroj pravdy pro stav kódu

\- handover dokument = přenos mezi chaty



\## Aktuální stav



Hotovo:

\- propojení s Trello

\- synchronizace výsledků

\- scheduler testován

\- persistentní stav providerů

\- orchestrator handoff opraven

\- run-id podpora dokončena



Ověřeno:

\- testy stabilizované

\- provider stav se ukládá

\- priority jsou definované



\---



\# 2. Prioritní systém



Používaná interpretace:



P5 = kritická / nejvyšší priorita  

P4 = velmi vysoká priorita  

P3 = vysoká priorita  

P2 = střední priorita  

P1 = nízká priorita  

P0 = nejnižší priorita / odložené



Audit musí ověřit, že:

\- Trello

\- AI Project Manager

\- AI Orchestrator



používají stejnou interpretaci priorit.



\---



\# 3. AI Orchestrator



\## Poslední důležitý commit



b545d94



Add run-id support for autonomous runs



\## Dokončeno



\### Run ID

Ověřeno:

\- CLI přijímá --run-id

\- service předává run\_id

\- logy používají správné ID

\- outbox používá správné ID



\### Provider fallback



Ověřen řetězec:



Claude Code

↓

Antigravity

↓

Codex



Chování:

\- Claude limit správně detekován

\- Antigravity limit správně detekován

\- fallback pokračuje na další provider



\---



\# 4. Stav providerů



\## Claude Code



Stav:

\- limit vyčerpán



Reset:

\- podle hlášení Claude Code



\## Antigravity



Stav:

\- OAuth přihlášení obnoveno

\- CLI funguje



Limit:

\- Individual quota reached



\## Codex



Stav:

\- spuštění proběhlo



Otevřený problém:

\- při interní chybě patchování vrací neúplný výstup

\- orchestrátor očekává task\_complete/error událost



\---



\# 5. AI Station Agent



\## Priorita



P4



Důvod:

\- není řídicí vrstva systému

\- čeká na stabilizaci AI Project Managera



Neprovádět větší autonomní běhy před dokončením AI Project Manager auditu.



\---



\# 6. Aktuální plán



Po obnovení provider limitů:



1\. Spustit kompletní audit AI Project Manager.

2\. Spustit kompletní testovací sadu.

3\. Provést skutečný autonomní E2E běh.

4\. Ověřit:

&#x20;  - scheduler

&#x20;  - resume

&#x20;  - checkpointy

&#x20;  - Trello synchronizaci

&#x20;  - priority

5\. Aktualizovat Trello.



Teprve potom pokračovat na další projekty.



\---



\# 7. Pravidla pro další práci



\- Neopakovat již vyřešené opravy.

\- Před změnou vždy ověřit aktuální stav.

\- Neprovádět změny pouze podle starých Slack zpráv.

\- Slack není zdroj pravdy.

\- Trello je zdroj pravdy pro stav projektu.

\- Git je zdroj pravdy pro stav kódu.



\---



\# Poslední známý dobrý stav



Datum:

2026-08-24



Stav:

AI Project Manager + AI Orchestrator jsou připravené na závěrečný audit a dlouhodobý autonomní test.


- [ ] Adapter musí používat lokálně dostupný OpenAI Codex CLI / Codex nástroj bez obcházení bezpečnostních kontrol.
<!-- Historický checklist z počáteční implementace Codex adaptéru. Není to aktuální
zdroj pravdy o v2; skutečné chování ověřují kód, testy a brokerový guide. -->

- [ ] Zachovat společný Agent/provider kontrakt používaný ai-orchestratorem.
- [ ] Umožnit výběr agenta přes `--agent codex`.
- [ ] Spouštět Codex v pracovním adresáři konkrétního projektu a nepovolit přístup mimo povolený workspace.
- [ ] Používat neinteraktivní režim vhodný pro autonomous běh a parsovat jeho strojově čitelný výstup.
- [ ] Jasně rozlišit SUCCESS, běžnou chybu providera a quota/session/rate-limit stav LIMITED.
- [ ] Pokud Codex poskytne retry/reset informaci, uložit ji do společného retry_after kontraktu.
- [ ] Zachytit timeout, návratový kód, stderr a nevalidní nebo neúplný výstup.
- [ ] Pokud jsou dostupné usage statistiky, ukládat input_tokens, output_tokens, thinking/reasoning_tokens a total_tokens.
- [ ] Agent nesmí vytvářet git commit; commitování zůstává odpovědností orchestrátoru.
- [ ] Zachovat existující bezpečnostní garance a nikdy nepoužívat ekvivalent neomezeného bypassu oprávnění.
- [ ] Přidat unit test úspěšného běhu.
- [ ] Přidat test chybového návratového kódu, nevalidního výstupu, timeoutu a quota/session limitu.
- [ ] Přidat integrační test přes mock CLI tak, aby testy nespotřebovávaly skutečnou Codex kvótu.
- [ ] Přidat volitelný explicitně spouštěný live read-only smoke test, který nezmění projekt.
- [ ] Ověřit, že registr/provider selection funguje přes `--agent codex`.
- [ ] Spustit celý test suite a všechny testy musí projít.

"""Isolated PoC package for evaluating "Hermes Agent" as a local/free provider.

Everything under `poc/hermes_agent/` is intentionally self-contained: it does
not import from `orchestrator.*` and is not registered in
`orchestrator/agents/registry.py` or wired into `runner.py`/`autonomous.py`.
It exists purely to answer the evaluation questions in `README.md` without
touching production code or production behaviour in any way. See
`poc/hermes_agent/README.md` for the full evaluation report and GO/NO-GO
recommendation.
"""

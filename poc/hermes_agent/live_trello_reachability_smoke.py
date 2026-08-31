"""Opt-in, live proof that the real Trello API is reachable and behaves per
its documented, credential-required contract (DoD point 2's "Trello"
sub-claim - see `integrations.py::check_trello_api_reachable` for full
rationale on why this, and not real board/card/label data, is what can
honestly be verified live from this sandbox: no Trello key/token exists
anywhere in this repository, Trello integration is owned entirely by an
external "AI Project Manager" tool).

Unlike `e2e_smoke.py` (always offline/deterministic, safe to run anywhere,
anytime), this script requires real internet access and talks to a real
third-party endpoint (`api.trello.com`) - so it is NEVER invoked
automatically by `e2e_smoke.py`, the default test suite, or any other
module in this PoC. Run it explicitly:

    python -m poc.hermes_agent.live_trello_reachability_smoke

Writes a JSON summary to
`poc/hermes_agent/.artifacts/live_trello_reachability_result.json`
(gitignored).
"""

from __future__ import annotations

import json
from pathlib import Path

from poc.hermes_agent.integrations import check_trello_api_reachable

ARTIFACTS_DIR = Path(__file__).resolve().parent / ".artifacts"


def main() -> dict:
    result = check_trello_api_reachable()
    summary = dict(result)
    # "ok" here means "the real API was reached and answered per its
    # documented, credential-required contract" - NOT "we accessed real
    # board data", which is out of scope without credentials (see README.md
    # "Zivé ověření dosažitelnosti Trello API").
    body = (result.get("body") or "").strip().lower()
    summary["ok"] = (
        result["reachable"]
        and result["http_status"] in (400, 401)
        and "invalid token" in body
    )

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS_DIR / "live_trello_reachability_result.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, ensure_ascii=False))

"""Opt-in authenticated, strictly read-only Trello verification.

Sending TRELLO_KEY/TRELLO_TOKEN to api.trello.com - even for a read-only GET -
is a one-time transmission of real credentials to a third-party service, and
that requires explicit, informed human sign-off each time it happens (see
AGENTS.md / README.md "Sandbox limitations"). An autonomous agent must never
grant that consent to itself just because the four TRELLO_* variables happen
to be present in the environment. `CONSENT_ENV_VAR` is therefore a second,
independent gate a human sets deliberately right before running this script -
it is not meant to be left on permanently or set by automation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from poc.hermes_agent.integrations import (
    TRELLO_CREDENTIAL_ENV_VARS,
    TRELLO_HUMAN_CONSENT_ENV_VAR,
    TRELLO_HUMAN_CONSENT_VALUE,
    trello_authenticated_read_gate_status,
    verify_trello_read_access,
)

ARTIFACT = Path(__file__).resolve().parent / ".artifacts" / "live_trello_read_result.json"

CONSENT_ENV_VAR = TRELLO_HUMAN_CONSENT_ENV_VAR
CONSENT_VALUE = TRELLO_HUMAN_CONSENT_VALUE


def main() -> dict:
    if os.environ.get(CONSENT_ENV_VAR) != CONSENT_VALUE:
        result = {
            "ok": False,
            "blocked_reason": (
                f"missing explicit human consent: set {CONSENT_ENV_VAR}="
                f"{CONSENT_VALUE} yourself right before running this script "
                "to confirm you want to send TRELLO_KEY/TRELLO_TOKEN to "
                "api.trello.com. An agent must never set this on its own."
            ),
            # Same offline, secret-free status e2e_smoke.py exposes, so this
            # artifact is self-descriptive without cross-referencing another
            # file/run to see whether credentials are even configured.
            "gate_status": trello_authenticated_read_gate_status(),
        }
    else:
        missing = [name for name in TRELLO_CREDENTIAL_ENV_VARS if not os.environ.get(name)]
        if missing:
            result = {"ok": False, "missing_environment_variables": missing}
        else:
            try:
                result = verify_trello_read_access(
                    key=os.environ["TRELLO_KEY"],
                    token=os.environ["TRELLO_TOKEN"],
                    board_id=os.environ["TRELLO_BOARD_ID"],
                    list_id=os.environ["TRELLO_INBOX_LIST"],
                )
                if result.get("ok") is True:
                    result["verification"] = "authenticated_read"
            except Exception as exc:
                # Never serialize exception text: HTTPError contains the
                # request URL, whose query carries the Trello key and token.
                result = {"ok": False, "error_type": type(exc).__name__}
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))

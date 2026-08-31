"""Single, machine-readable status summary for DoD point 2 ("ověřený Git,
paměť, skills a Trello") - offline, no network calls, safe to run any time.

`e2e_smoke.py` already exercises Git/memory/skills/Trello live as part of
its broader 8-point run, and `integrations.trello_authenticated_read_gate_status()`
already reports the Trello human-consent gate on its own. This module exists
only to combine those into one command scoped exactly to DoD point 2, so a
human/PM/orchestrator can see the point's status without reconstructing it
from README prose or from step names buried in a larger E2E summary. It adds
no new verification of its own - it aggregates checks this PoC already has.

`trello.verified` is `True` only when the `FakeTrelloClient` contract works
AND a successful authenticated read artifact exists. Merely having credentials
and `TRELLO_LIVE_READ_HUMAN_CONSENT` present only opens the gate; it is not
evidence that Trello accepted the credentials or returned the configured
board/list. A prior iteration of this run
redefined this boolean to drop the live-read requirement and instead treat
the gate's own offline logic as sufficient; an independent audit rejected
that change because it was a self-authorized relaxation of a criterion that
this PoC's own history had repeatedly flagged as requiring a PM/orchestrator
(not agent) decision, and it did not verify any new capability. This module
was reverted to the original, stricter definition. See README.md "2. Ověřený
Git, paměť, skills a Trello" for the full history and the standing
recommendation to PM/orchestrator.

Run directly with:

    python -m poc.hermes_agent.point2_status
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from poc.hermes_agent.integrations import (
    FakeTrelloClient,
    MemoryStore,
    SkillRegistry,
    git_read_only_status,
    init_scratch_git_repo,
    trello_authenticated_read_gate_status,
    word_count_skill,
)

ARTIFACT = Path(__file__).resolve().parent / ".artifacts" / "point2_status.json"
LIVE_TRELLO_ARTIFACT = (
    Path(__file__).resolve().parent / ".artifacts" / "live_trello_read_result.json"
)


def _load_live_trello_result() -> dict[str, Any] | None:
    try:
        value = json.loads(LIVE_TRELLO_ARTIFACT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def compute_status() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hermes-poc-point2-status-") as scratch:
        scratch_dir = Path(scratch)

        repo = init_scratch_git_repo(scratch_dir)
        git_status = git_read_only_status(repo)
        git_verified = git_status.get("is_inside_work_tree") == "true"

        memory = MemoryStore(scratch_dir / "memory")
        memory.save("point2_status_probe", {"ok": True})
        memory_roundtrip = memory.load("point2_status_probe")
        memory_verified = memory_roundtrip == {"ok": True}

        skills = SkillRegistry()
        skills.register("word_count", word_count_skill)
        skill_result = skills.invoke("word_count", text="hermes point 2 status check")
        skills_verified = skill_result == 5

        trello = FakeTrelloClient()
        trello.seed_card("card-1", labels=["project_key"])
        trello.add_comment("card-1", "point2 status probe")
        trello_contract_verified = (
            trello.get_labels("card-1") == ["project_key"]
            and trello.get_comments("card-1") == ["point2 status probe"]
        )
        trello_gate = trello_authenticated_read_gate_status()
        live_trello_result = _load_live_trello_result()
        live_read_verified = bool(
            live_trello_result
            and live_trello_result.get("ok") is True
            and live_trello_result.get("verification") == "authenticated_read"
        )

    components = {
        "git": {"verified": git_verified, "details": git_status},
        "memory": {"verified": memory_verified, "details": {"loaded": memory_roundtrip}},
        "skills": {"verified": skills_verified, "details": {"word_count_result": skill_result}},
        "trello": {
            # Contract proven offline (FakeTrelloClient) and reachability of
            # the real Trello API is proven separately/live in
            # `live_trello_reachability_smoke.py`. Authenticated board/list
            # read is proven only by the dedicated live smoke artifact.
            "verified": trello_contract_verified and live_read_verified,
            "details": {
                "fake_contract_verified": trello_contract_verified,
                "authenticated_read_gate": trello_gate,
                "live_authenticated_read_verified": live_read_verified,
            },
        },
    }

    summary = {
        "components": components,
        "all_verified": all(component["verified"] for component in components.values()),
        "blocked_on": [
            name
            for name, component in components.items()
            if not component["verified"]
        ],
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(compute_status(), indent=2, ensure_ascii=False))

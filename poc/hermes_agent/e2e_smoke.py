"""Live, harmless end-to-end proof for the Hermes Agent PoC (DoD point 7).

Runs every PoC module together against real code paths (real subprocess Git
calls, real file I/O, real wall-clock timing) but exclusively inside a fresh
`tempfile.mkdtemp()` sandbox plus this PoC's own `.artifacts/` directory -
never against this repository, never against a real network, and never with
a destructive command (see `security.py`). Run directly with:

    python -m poc.hermes_agent.e2e_smoke

Writes a JSON summary to `poc/hermes_agent/.artifacts/e2e_smoke_result.json`
and returns the same dict, so both a human and `tests/` can inspect exactly
what happened on the last run.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from poc.hermes_agent.adapter import FakeLocalTransport, HermesAgent, HermesRunRequest
from poc.hermes_agent.benchmark import run_benchmark
from poc.hermes_agent.fallback import HermesFailover
from poc.hermes_agent.integrations import (
    FakeTrelloClient,
    MemoryStore,
    SkillRegistry,
    git_read_only_status,
    init_scratch_git_repo,
    trello_authenticated_read_gate_status,
    word_count_skill,
)
from poc.hermes_agent.security import (
    SecurityBoundaryError,
    ensure_within_workspace,
)

ARTIFACTS_DIR = Path(__file__).resolve().parent / ".artifacts"


def main() -> dict[str, Any]:
    summary: dict[str, Any] = {"steps": []}

    with tempfile.TemporaryDirectory(prefix="hermes-poc-e2e-") as scratch:
        scratch_dir = Path(scratch)

        # 1. Local/free provider run (point 1)
        agent = HermesAgent(transport=FakeLocalTransport(), workspace_root=scratch_dir)
        result = agent.run(HermesRunRequest(project_path=scratch_dir, prompt="ping"))
        summary["steps"].append({
            "step": "local_provider_run",
            "success": result.success,
            "latency_seconds": result.latency_seconds,
            "cost_usd": result.cost_usd,
        })

        # 2. Git / memory / skills / Trello (point 2)
        repo = init_scratch_git_repo(scratch_dir)
        git_status = git_read_only_status(repo)
        summary["steps"].append({"step": "git_read_only_status", "result": git_status})

        memory = MemoryStore(scratch_dir / "memory")
        memory.save("run_note", {"note": "e2e smoke run"})
        summary["steps"].append({
            "step": "memory_roundtrip",
            "loaded": memory.load("run_note"),
            "keys": memory.list_keys(),
        })

        skills = SkillRegistry()
        skills.register("word_count", word_count_skill)
        summary["steps"].append({
            "step": "skill_invocation",
            "result": skills.invoke("word_count", text="hello from hermes poc"),
        })

        trello = FakeTrelloClient()
        trello.seed_card("card-1", labels=["project_key"])
        trello.add_comment("card-1", "hermes poc e2e smoke comment")
        summary["steps"].append({
            "step": "trello_fake_contract",
            "labels": trello.get_labels("card-1"),
            "comments": trello.get_comments("card-1"),
            # Offline, no network call: reports whether an authenticated live
            # read is currently blocked and why, without exposing secrets or
            # granting consent on the agent's behalf (see integrations.py).
            "authenticated_read_gate": trello_authenticated_read_gate_status(),
        })

        # 3. Fallback/recovery and limits (point 3)
        limited_first = HermesAgent(
            transport=FakeLocalTransport(fail_after=0), workspace_root=scratch_dir
        )
        healthy_second = HermesAgent(
            transport=FakeLocalTransport(), workspace_root=scratch_dir
        )
        failover = HermesFailover(providers=[limited_first, healthy_second], max_total_calls=5)
        failover_result = failover.run(HermesRunRequest(project_path=scratch_dir, prompt="failover check"))
        summary["steps"].append({
            "step": "fallback_recovery",
            "provider_used": failover_result.provider_name,
            "attempts": failover_result.attempts,
            "success": failover_result.result.success,
        })

        # 4. Quality/speed/VRAM/cost measurement (point 4)
        # vram_mb is a real nvidia-smi sample (GPU-wide, not attributed to a
        # specific model process - see benchmark.py docstring). quality_score
        # is a real keyword-overlap score of FakeLocalTransport's canned
        # output, proving the scoring methodology, not a real model's quality.
        bench = run_benchmark(
            agent,
            prompts=["p1", "p2", "p3"],
            project_path=scratch_dir,
            quality_keywords=["fake", "hermes"],
            sample_vram=True,
        )
        summary["steps"].append({
            "step": "benchmark",
            "mean_latency_seconds": bench.mean_latency_seconds,
            "success_rate": bench.success_rate,
            "total_cost_usd": bench.total_cost_usd,
            "vram_mb": bench.vram_mb,
            "quality_score": bench.quality_score,
        })

        # 5. Security boundaries (point 5)
        boundary_violations_rejected = 0
        calls_before_rejections = agent.transport.calls
        for dangerous_command in ("git push --force", "git reset --hard", "rm -rf /"):
            rejected = agent.run(
                HermesRunRequest(project_path=scratch_dir, prompt=dangerous_command)
            )
            if not rejected.success and rejected.error and "security boundary" in rejected.error:
                boundary_violations_rejected += 1
        try:
            ensure_within_workspace(Path("C:/outside/workspace"), scratch_dir)
            outside_path_rejected = False
        except SecurityBoundaryError:
            outside_path_rejected = True
        summary["steps"].append({
            "step": "security_boundaries",
            "dangerous_commands_rejected": boundary_violations_rejected,
            "dangerous_commands_checked": 3,
            "transport_calls_for_rejected_commands": agent.transport.calls - calls_before_rejections,
            "outside_workspace_path_rejected": outside_path_rejected,
        })

    summary["ok"] = all(
        step.get("success", True) if isinstance(step.get("success"), bool) else True
        for step in summary["steps"]
    )

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS_DIR / "e2e_smoke_result.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2, ensure_ascii=False))

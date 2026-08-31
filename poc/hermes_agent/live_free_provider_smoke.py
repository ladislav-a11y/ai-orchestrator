"""Opt-in, live proof that a genuinely free provider works end-to-end (DoD
points 1 and 4) - makes exactly ONE real, harmless, $0-cost network call to
Hermes Agent's own built-in keyless "OpenCode Free" provider
(`OpenCodeFreeTransport`, see `adapter.py` for the full provenance).

Unlike `e2e_smoke.py` (always offline/deterministic, safe to run anywhere,
anytime), this script requires real internet access and talks to a real
third-party endpoint - so it is NEVER invoked automatically by
`e2e_smoke.py`, the default test suite, or any other module in this PoC.
Run it explicitly:

    python -m poc.hermes_agent.live_free_provider_smoke

Writes a JSON summary to
`poc/hermes_agent/.artifacts/live_free_provider_result.json` (gitignored).
"""

from __future__ import annotations

import json
from pathlib import Path

from poc.hermes_agent.adapter import HermesAgent, HermesRunRequest, OpenCodeFreeTransport
from poc.hermes_agent.benchmark import sample_gpu_vram_mb, score_quality

ARTIFACTS_DIR = Path(__file__).resolve().parent / ".artifacts"

PROMPT = "Reply with exactly one word: pong"
QUALITY_KEYWORDS = ["pong"]


def main() -> dict:
    agent = HermesAgent(transport=OpenCodeFreeTransport())
    # The model executes remotely, so server-side VRAM is not observable.
    # Capture the real client GPU before and after the call and label its
    # scope explicitly instead of omitting or misattributing this metric.
    vram_before_mb = sample_gpu_vram_mb()

    available, availability_message = agent.is_available()
    summary: dict = {
        "provider": agent.transport.name,
        "model": agent.transport.model,
        "available": available,
        "availability_message": availability_message,
    }

    if not available:
        summary["ok"] = False
        summary["reason"] = "provider reported unavailable, no call attempted"
    else:
        result = agent.run(HermesRunRequest(project_path=Path.cwd(), prompt=PROMPT))
        vram_after_mb = sample_gpu_vram_mb()
        summary["success"] = result.success
        summary["output_text"] = result.output_text
        summary["cost_usd"] = result.cost_usd
        summary["latency_seconds"] = result.latency_seconds
        summary["limited"] = result.limited
        summary["error"] = result.error
        summary["usage"] = result.raw.get("usage")
        summary["quality_score"] = (
            score_quality(result.output_text, QUALITY_KEYWORDS) if result.success else None
        )
        summary["vram"] = {
            "applicability": "client_gpu_only_remote_provider",
            "provider_vram_mb": None,
            "client_gpu_before_mb": vram_before_mb,
            "client_gpu_after_mb": vram_after_mb,
            "client_gpu_delta_mb": (
                vram_after_mb - vram_before_mb
                if vram_before_mb is not None and vram_after_mb is not None
                else None
            ),
        }
        summary["ok"] = result.success and result.cost_usd == 0.0

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS_DIR / "live_free_provider_result.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, ensure_ascii=False))

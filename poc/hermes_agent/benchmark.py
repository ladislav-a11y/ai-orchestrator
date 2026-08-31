"""Quality/speed/VRAM/cost measurement harness for the Hermes Agent PoC (DoD
point 4).

Speed and cost are real, measured numbers (wall-clock latency and the
transport-reported `cost_usd`) for whatever transport is passed in.

VRAM sampling (`sample_gpu_vram_mb`) is real too - it shells out to the
real, read-only `nvidia-smi --query-gpu=memory.used` and was confirmed
working in this sandbox against an actual NVIDIA GeForce RTX 2060 (6144 MiB
total). What it honestly is NOT: an attribution of that memory to a specific
local model process, because no local model process is running in this
sandbox (no `ollama`/local GGUF runtime is installed here - see
`README.md` "Sandbox limitations"). `run_benchmark(..., sample_vram=True)`
therefore reports the GPU's current overall memory usage at call time, not
"this provider used N MB" - a real number, honestly scoped.

Quality scoring (`score_quality`) is a real, deterministic keyword-overlap
heuristic - it proves the measurement *methodology* works end-to-end
(callable, tested, produces a 0..1 score from real output text), but when
run against `FakeLocalTransport`'s canned response it is scoring a fake
answer, not a real model's reasoning - see README.md for what a real
quality pass needs (an actual provider call, which this PoC deliberately
never makes - see security boundaries).

Fabricating a "real model" quality/VRAM number here would be worse than
being explicit about what was and wasn't actually measured.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Optional

from poc.hermes_agent.adapter import HermesAgent, HermesRunRequest


@dataclass
class BenchmarkReport:
    provider_name: str
    iterations: int
    latencies_seconds: list[float] = field(default_factory=list)
    total_cost_usd: float = 0.0
    failures: int = 0
    # Real GPU-wide sample when requested (see module docstring for scope);
    # None when not requested or when nvidia-smi is unavailable.
    vram_mb: Optional[float] = None
    # Real keyword-overlap score when quality_keywords is passed; None
    # otherwise (see module docstring - methodology-only, not a real-model
    # quality claim unless the transport is a real provider).
    quality_score: Optional[float] = None

    @property
    def mean_latency_seconds(self) -> float:
        return mean(self.latencies_seconds) if self.latencies_seconds else 0.0

    @property
    def success_rate(self) -> float:
        if self.iterations == 0:
            return 0.0
        return (self.iterations - self.failures) / self.iterations


def sample_gpu_vram_mb() -> Optional[float]:
    """Real, read-only sample of current GPU memory usage via `nvidia-smi`.
    Returns None (never raises) if `nvidia-smi` is not on PATH or the call
    fails - absence of a GPU/driver is a legitimate, common environment, not
    an error condition for this PoC."""
    path = shutil.which("nvidia-smi")
    if not path:
        return None
    try:
        completed = subprocess.run(
            [path, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    try:
        return float(first_line.strip())
    except ValueError:
        return None


def score_quality(output_text: str, expected_keywords: list[str]) -> float:
    """Fraction of `expected_keywords` (case-insensitive) present in
    `output_text`. Simple on purpose - this is a stand-in methodology to
    prove the harness end-to-end, not a claim of a rigorous eval."""
    if not expected_keywords:
        return 0.0
    lowered = output_text.lower()
    hits = sum(1 for keyword in expected_keywords if keyword.lower() in lowered)
    return hits / len(expected_keywords)


def run_benchmark(
    agent: HermesAgent,
    prompts: list[str],
    project_path: Path,
    quality_keywords: Optional[list[str]] = None,
    sample_vram: bool = False,
) -> BenchmarkReport:
    report = BenchmarkReport(provider_name=agent.name, iterations=len(prompts))
    if sample_vram:
        report.vram_mb = sample_gpu_vram_mb()

    quality_scores: list[float] = []
    for prompt in prompts:
        result = agent.run(HermesRunRequest(project_path=project_path, prompt=prompt))
        if result.latency_seconds is not None:
            report.latencies_seconds.append(result.latency_seconds)
        if result.cost_usd:
            report.total_cost_usd += result.cost_usd
        if not result.success:
            report.failures += 1
        elif quality_keywords is not None:
            quality_scores.append(score_quality(result.output_text, quality_keywords))

    if quality_scores:
        report.quality_score = mean(quality_scores)
    return report

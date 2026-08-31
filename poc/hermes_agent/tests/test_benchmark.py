import subprocess
from pathlib import Path

from poc.hermes_agent.adapter import FakeLocalTransport, HermesAgent
from poc.hermes_agent.benchmark import run_benchmark, sample_gpu_vram_mb, score_quality


def test_run_benchmark_measures_latency_and_cost(tmp_path: Path):
    agent = HermesAgent(transport=FakeLocalTransport(), workspace_root=tmp_path)
    report = run_benchmark(agent, prompts=["a", "bb", "ccc"], project_path=tmp_path)

    assert report.iterations == 3
    assert report.failures == 0
    assert report.success_rate == 1.0
    assert len(report.latencies_seconds) == 3
    assert report.mean_latency_seconds >= 0
    assert report.total_cost_usd == 0.0
    # Honestly unmeasured in this sandbox - see benchmark.py module docstring.
    assert report.vram_mb is None
    assert report.quality_score is None


def test_run_benchmark_counts_failures(tmp_path: Path):
    agent = HermesAgent(transport=FakeLocalTransport(fail_after=1), workspace_root=tmp_path)
    report = run_benchmark(agent, prompts=["a", "b", "c"], project_path=tmp_path)

    assert report.iterations == 3
    assert report.failures == 2
    assert abs(report.success_rate - (1 / 3)) < 1e-9


def test_score_quality_counts_keyword_overlap():
    assert score_quality("OK: fake local Hermes response", ["fake", "hermes"]) == 1.0
    assert score_quality("OK: fake local Hermes response", ["fake", "missing-word"]) == 0.5
    assert score_quality("nothing relevant here", ["fake", "hermes"]) == 0.0
    assert score_quality("anything", []) == 0.0


def test_sample_gpu_vram_mb_returns_none_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert sample_gpu_vram_mb() is None


def test_sample_gpu_vram_mb_parses_real_nvidia_smi_output_shape(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "nvidia-smi")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0, stdout="996\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert sample_gpu_vram_mb() == 996.0


def test_sample_gpu_vram_mb_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "nvidia-smi")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no device")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert sample_gpu_vram_mb() is None


def test_run_benchmark_with_quality_keywords_scores_fake_transport_output(tmp_path: Path):
    agent = HermesAgent(
        transport=FakeLocalTransport(canned_response="OK: fake local Hermes response"),
        workspace_root=tmp_path,
    )
    report = run_benchmark(
        agent,
        prompts=["hi"],
        project_path=tmp_path,
        quality_keywords=["fake", "hermes"],
    )
    assert report.quality_score == 1.0


def test_run_benchmark_sample_vram_true_uses_real_sampler(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("poc.hermes_agent.benchmark.sample_gpu_vram_mb", lambda: 1234.0)
    agent = HermesAgent(transport=FakeLocalTransport(), workspace_root=tmp_path)
    report = run_benchmark(agent, prompts=["hi"], project_path=tmp_path, sample_vram=True)
    assert report.vram_mb == 1234.0

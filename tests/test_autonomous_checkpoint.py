import json
import logging

from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import (
    DOD_BATCH_SIZE,
    AutonomousStatus,
    parse_definition_of_done,
    run_autonomous_loop,
)
from orchestrator.autonomous_checkpoint import (
    apply_checkpoint,
    apply_pm_checkpoint,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from orchestrator.config import Config

LOGGER = logging.getLogger("test")


class FakeAgent(Agent):
    name = "fake"

    def __init__(self, run_fn):
        self._run_fn = run_fn

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        return self._run_fn(request)


def _spec_text(total_items: int) -> str:
    return "\n".join(f"- [ ] bod {i}" for i in range(total_items))


def _batch_marking_executor(dod_items, batch_size: int = DOD_BATCH_SIZE):
    """Marks exactly the batch of DoD items currently unmet (mirrors
    `_select_batch`) as done, in order - equivalent to "an executor that
    always honestly and fully completes whatever batch it is asked about".
    Reads `dod_items` live (the same list object the loop mutates), so it
    stays correct no matter what state the list already started in (e.g.
    after a checkpoint restore)."""

    def run_fn(request):
        indices = [idx for idx, item in enumerate(dod_items) if not item.done][:batch_size]
        payload = {"items": [{"index": idx, "done": True} for idx in indices], "notes": "davka hotova"}
        return AgentRunResult(success=True, output_text=json.dumps(payload))

    return run_fn


# -- module-level round trip ---------------------------------------------


def test_save_and_load_checkpoint_round_trip(tmp_path):
    project_path = tmp_path / "proj"
    project_path.mkdir()
    dod_source = _spec_text(4)
    dod_items = parse_definition_of_done(dod_source)
    dod_items[0].done = True
    dod_items[2].done = True

    data_dir = tmp_path / "data"
    usage_events = [{"provider": "codex", "input_tokens": 10, "stage": "executor"}]
    usage_by_provider = {"codex": {"input_tokens": 10, "source": "reported"}}
    usage_total = {"input_tokens": 10, "source": "reported"}
    save_checkpoint(
        data_dir, project_path, dod_source, "cil", dod_items, "run-1",
        usage_events, usage_by_provider, usage_total,
    )

    checkpoint = load_checkpoint(data_dir, project_path, dod_source)
    assert checkpoint is not None
    assert checkpoint.usage_events == usage_events
    assert checkpoint.usage_by_provider == usage_by_provider
    assert checkpoint.usage_total == usage_total

    fresh = parse_definition_of_done(dod_source)
    restored = apply_checkpoint(fresh, checkpoint)

    assert restored == 2
    assert [i.done for i in fresh] == [True, False, True, False]


def test_load_checkpoint_missing_returns_none(tmp_path):
    project_path = tmp_path / "proj"
    project_path.mkdir()
    assert load_checkpoint(tmp_path / "data", project_path, _spec_text(3)) is None


def test_apply_checkpoint_never_partially_applies_on_text_mismatch(tmp_path):
    """Defense in depth: even if a checkpoint's stored items somehow do not
    line up 1:1 with the freshly parsed DoD list (should never happen given
    a matching spec_hash - see checkpoint_path - but must never be trusted
    blindly), apply_checkpoint must apply nothing rather than mis-map one
    item's done-state onto a different item."""
    from orchestrator.autonomous_checkpoint import DoDCheckpoint

    fresh = parse_definition_of_done(_spec_text(3))
    corrupted = DoDCheckpoint(
        project_path="x",
        spec_hash="x",
        goal="cil",
        items=[{"text": "bod 0", "done": True}, {"text": "JINY TEXT", "done": True}, {"text": "bod 2", "done": False}],
        run_id="run-1",
        saved_at="",
    )
    restored = apply_checkpoint(fresh, corrupted)
    assert restored == 0
    assert all(not i.done for i in fresh)


def test_five_item_checkpoint_rejects_out_of_range_indices_and_preserves_existing_done():
    """Stale indices 64-65 must not map onto a five-item PM handoff DoD."""
    from orchestrator.autonomous_checkpoint import DoDCheckpoint

    fresh = parse_definition_of_done(_spec_text(5))
    fresh[0].done = True
    stale_items = [
        {"index": index, "text": item.text, "done": True}
        for index, item in enumerate(fresh)
    ]
    stale_items[3]["index"] = 64
    stale_items[4]["index"] = 65
    checkpoint = DoDCheckpoint(
        project_path="ignored",
        spec_hash="ignored",
        goal="cil",
        items=stale_items,
        run_id="old-run",
        saved_at="ignored",
    )

    assert apply_checkpoint(fresh, checkpoint) == 0
    assert [item.done for item in fresh] == [True, False, False, False, False]


def test_apply_checkpoint_rejects_non_boolean_done_value():
    from orchestrator.autonomous_checkpoint import DoDCheckpoint

    fresh = parse_definition_of_done(_spec_text(1))
    checkpoint = DoDCheckpoint(
        project_path="ignored",
        spec_hash="ignored",
        goal="cil",
        items=[{"text": fresh[0].text, "done": "false"}],
        run_id="old-run",
        saved_at="ignored",
    )

    assert apply_checkpoint(fresh, checkpoint) == 0
    assert fresh[0].done is False


# -- invalidation on spec change (requirement 8) ------------------------


def test_checkpoint_invalidated_when_spec_content_changes(tmp_path):
    project_path = tmp_path / "proj"
    project_path.mkdir()
    data_dir = tmp_path / "data"

    spec_v1 = _spec_text(5)
    items_v1 = parse_definition_of_done(spec_v1)
    items_v1[0].done = True
    save_checkpoint(data_dir, project_path, spec_v1, "cil", items_v1, "run-1")

    assert load_checkpoint(data_dir, project_path, spec_v1) is not None

    # Same project, but the spec checklist content changed (one item text
    # edited) - must not resolve to the same checkpoint at all.
    spec_v2 = spec_v1.replace("bod 0", "bod 0 upraveny")
    assert checkpoint_path(data_dir, project_path, spec_v1) != checkpoint_path(data_dir, project_path, spec_v2)
    assert load_checkpoint(data_dir, project_path, spec_v2) is None

    fresh_v2 = parse_definition_of_done(spec_v2)
    checkpoint_v2 = load_checkpoint(data_dir, project_path, spec_v2)
    restored = apply_checkpoint(fresh_v2, checkpoint_v2) if checkpoint_v2 else 0
    assert restored == 0
    assert all(not i.done for i in fresh_v2)


def test_live_result_updates_do_not_change_checkpoint_identity(tmp_path):
    declaration = (
        '- [ ] Produkcni stav '
        '<!-- LIVE-EVIDENCE: {"command":"read health","expect":"ready"} -->'
    )
    with_result = declaration + (
        '\n<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"ready"} -->'
    )
    assert checkpoint_path(tmp_path, tmp_path / "project", declaration) == checkpoint_path(
        tmp_path, tmp_path / "project", with_result
    )


def test_checkpoint_round_trips_live_evidence(tmp_path):
    project = tmp_path / "project"
    spec = (
        '- [x] Produkcni stav '
        '<!-- LIVE-EVIDENCE: {"command":"read health","expect":"ready"} -->\n'
        '<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"ready now"} -->'
    )
    original = parse_definition_of_done(spec)
    save_checkpoint(tmp_path, project, spec, "cil", original, "run-live")
    restored_items = parse_definition_of_done(spec.splitlines()[0])
    checkpoint = load_checkpoint(tmp_path, project, spec.splitlines()[0])
    assert checkpoint is not None
    assert apply_checkpoint(restored_items, checkpoint) == 1
    assert restored_items[0].done is True
    assert restored_items[0].live_evidence["passed"] is True

def test_checkpoint_survives_pm_checkpoint_run_id_churn(tmp_path):
    """Regression for a real bug found in the live queue: AI Project Manager
    appends a trailing "<!-- PM-CHECKPOINT {"run_id": ...} -->" comment to
    the spec text and mints a NEW random run_id in it on every scheduler
    tick that resubmits the same card - so two spec_text values for "the
    same" card differ only inside that block. Hashing the raw text (as
    checkpoint_path/load_checkpoint/save_checkpoint used to) would silently
    discard verified DoD progress on every single tick instead of resuming
    it.
    """
    project_path = tmp_path / "proj"
    project_path.mkdir()
    data_dir = tmp_path / "data"

    tick_1 = (
        _spec_text(4)
        + '\n\n<!-- PM-CHECKPOINT\n{\n  "run_id": "aaaaaaaaaaaa"\n}\n-->\n'
    )
    tick_2 = (
        _spec_text(4)
        + '\n\n<!-- PM-CHECKPOINT\n{\n  "run_id": "bbbbbbbbbbbb"\n}\n-->\n'
    )
    assert tick_1 != tick_2

    items = parse_definition_of_done(tick_1)
    items[0].done = True
    save_checkpoint(data_dir, project_path, tick_1, "cil", items, "run-1")

    assert checkpoint_path(data_dir, project_path, tick_1) == checkpoint_path(data_dir, project_path, tick_2)

    checkpoint = load_checkpoint(data_dir, project_path, tick_2)
    assert checkpoint is not None

    fresh = parse_definition_of_done(tick_2)
    restored = apply_checkpoint(fresh, checkpoint)
    assert restored == 1
    assert fresh[0].done is True


def test_explicit_empty_pm_checkpoint_suppresses_stale_local_progress():
    spec = (
        _spec_text(3)
        + '\n\n<!-- PM-CHECKPOINT\n'
        + '{"run_id":"new-run","checkpoint":{}}\n-->\n'
    )
    fresh = parse_definition_of_done(spec)

    restored = apply_pm_checkpoint(fresh, spec)

    assert restored == 0
    assert all(not item.done for item in fresh)


def test_explicit_pm_checkpoint_restores_only_declared_indices():
    spec = (
        _spec_text(3)
        + '\n\n<!-- PM-CHECKPOINT\n'
        + '{"run_id":"new-run","checkpoint":{"completed_dod_indices":[1]}}\n-->\n'
    )
    fresh = parse_definition_of_done(spec)

    restored = apply_pm_checkpoint(fresh, spec)

    assert restored == 1
    assert [item.done for item in fresh] == [False, True, False]


def test_checkpoint_scoped_per_project_not_shared(tmp_path):
    data_dir = tmp_path / "data"
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    spec = _spec_text(3)

    items_a = parse_definition_of_done(spec)
    items_a[0].done = True
    save_checkpoint(data_dir, project_a, spec, "cil", items_a, "run-1")

    assert load_checkpoint(data_dir, project_b, spec) is None


# -- full regression: persistence across two separate run_autonomous_loop --
# -- invocations (requirement 7) ------------------------------------------


def test_autonomous_checkpoint_persists_across_separate_runs(tmp_path):
    """Regression for the reported bug: run 40973d234df7 processed 2
    iterations (66 -> 50 unmet DoD items, i.e. 16 done) and stopped; the next
    run (a1417dd9328d) started over from batch 0 because nothing persisted
    the verified DoD state between the two separate process invocations.

    This simulates exactly that: "run 1" is a run_autonomous_loop call that
    stops (here: hits max_iterations, standing in for any interruption -
    Ctrl+C, a session limit, a crash) after marking 16/66 items done, saving
    a checkpoint after every iteration via `on_iteration` (exactly how
    OrchestratorService.run_autonomous wires it). "run 2" is a brand new
    process: a freshly parsed DoD list (all items not-done again) that loads
    and applies the checkpoint before doing anything else, and must resume
    at the next unmet batch, not at index 0.
    """
    project_path = tmp_path / "proj"
    project_path.mkdir()
    data_dir = tmp_path / "data"
    total_items = 66
    spec = _spec_text(total_items)
    cfg = Config()

    # -- run 1: two iterations, marks 16/66 items done, then "the process
    # ends" (here: max_iterations reached) -------------------------------
    dod_items_run1 = parse_definition_of_done(spec)
    checkpoint_snapshots = []

    def on_iteration(partial_result):
        save_checkpoint(data_dir, project_path, spec, "cil", partial_result.dod_items, "run-1")
        reloaded = load_checkpoint(data_dir, project_path, spec)
        checkpoint_snapshots.append(sum(1 for i in reloaded.items if i["done"]))

    result1 = run_autonomous_loop(
        run_id="run-1",
        project_path=project_path,
        goal="cil",
        dod_items=dod_items_run1,
        config=cfg,
        agent=FakeAgent(_batch_marking_executor(dod_items_run1)),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=False,
        on_iteration=on_iteration,
    )

    assert result1.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result1.iterations) == 2
    done_count_run1 = sum(1 for i in result1.dod_items if i.done)
    assert done_count_run1 == 16
    unmet_run1 = total_items - done_count_run1
    assert unmet_run1 == 50

    # Checkpoint was persisted incrementally, after each iteration - not
    # only once at the very end of the run. run_autonomous_loop invokes
    # on_iteration once per completed iteration, plus once more for the
    # final status snapshot (see its docstring / RUNNING vs terminal calls).
    assert checkpoint_snapshots == [DOD_BATCH_SIZE, 16, 16]

    # -- run 2: brand new process - freshly parsed DoD list starts at
    # all-not-done again, exactly like a new `orchestrator autonomous ...`
    # invocation would produce -------------------------------------------
    dod_items_run2 = parse_definition_of_done(spec)
    assert all(not i.done for i in dod_items_run2)

    checkpoint = load_checkpoint(data_dir, project_path, spec)
    assert checkpoint is not None
    restored = apply_checkpoint(dod_items_run2, checkpoint)
    assert restored == 16
    assert sum(1 for i in dod_items_run2 if i.done) == 16

    result2 = run_autonomous_loop(
        run_id="run-2",
        project_path=project_path,
        goal="cil",
        dod_items=dod_items_run2,
        config=cfg,
        agent=FakeAgent(_batch_marking_executor(dod_items_run2)),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    # Must resume at the next unmet batch (indices 16..23), never re-request
    # the already-verified indices 0..15.
    assert result2.iterations[0].requested_indices == list(range(16, 16 + DOD_BATCH_SIZE))
    assert 0 not in result2.iterations[0].requested_indices

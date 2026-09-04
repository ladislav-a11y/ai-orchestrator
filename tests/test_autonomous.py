import json
import logging
import re
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import (
    ABSOLUTE_MAX_ITERATIONS,
    AUDIT_MARKER,
    AutonomousStatus,
    DOD_BATCH_SIZE,
    HERMES_DOD_BATCH_SIZE,
    NO_PROGRESS_LIMIT,
    PROTOCOL_ERROR_STREAK_LIMIT,
    _extract_json,
    _build_audit_prompt,
    _audit_evidence_has_project_scope,
    _audit_needs_quality_fallback,
    _controller_audit_gate_indices,
    _validate_audit_response,
    controller_finalization_from_spec,
    parse_definition_of_done,
    run_autonomous_loop,
)
from orchestrator.config import Config, GitConfig

LOGGER = logging.getLogger("test")


def _audit_response(request, *, accepted=True, index=None, evidence="audit evidence", method="static: kontrola projektu"):
    indices = [int(value) for value in re.findall(r"(?m)^(\d+)\. ", request.prompt)]
    if index is not None:
        indices = [index]
    if evidence == "audit evidence":
        evidence = f"{request.project_path.name}: audit evidence"
    return json.dumps({
        "items": [
            {"index": value, "accepted": accepted, "method": method, "evidence": evidence}
            for value in indices
        ],
        "notes": "audit ok",
    })


class FakeAgent(Agent):
    name = "fake"

    def __init__(self, run_fn):
        self._run_fn = run_fn

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        return self._run_fn(request)


def _confirming_audit_or(executor_run_fn):
    """Wrap an executor run_fn so any independent-audit prompt (see
    AUDIT_MARKER) gets a clean "everything confirmed" response, and every
    other prompt goes to the given executor run_fn. Most tests only care
    about the executor's behaviour; the audit pass is a separate, real
    part of the contract (see AGENTS.md rule 8) so it must not be silently
    skipped by a fake that doesn't know about it."""

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(success=True, output_text=_audit_response(request))
        return executor_run_fn(request)

    return run_fn


def test_orchestrator_owns_executor_and_audit_output_contracts(tmp_path):
    requests = []

    def run_fn(request):
        requests.append(request)
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text=_audit_response(request),
            )
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    result = run_autonomous_loop(
        run_id="schema-contract",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] bod A"),
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert len(requests) == 2
    assert requests[0].output_schema["properties"]["items"]["items"]["properties"]["index"]["enum"] == [0]
    audit_items = requests[1].output_schema["properties"]["items"]
    assert audit_items["minItems"] == audit_items["maxItems"] == 1
    assert audit_items["items"]["properties"]["index"]["enum"] == [0]
    # Codex native structured outputs support only a strict JSON Schema
    # subset. ``uniqueItems`` makes ``codex exec`` reject the audit schema
    # before the auditor can run. Duplicate indices are normalized by the
    # audit parser already, so that unsupported keyword is unnecessary.
    assert "uniqueItems" not in audit_items
    assert AUDIT_MARKER not in requests[0].prompt
    assert AUDIT_MARKER in requests[1].prompt


def test_implementation_only_completes_before_audit(tmp_path):
    requests = []

    def run_fn(request):
        requests.append(request)
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    result = run_autonomous_loop(
        run_id="implementation-only",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] bod A"),
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
        implementation_only=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert len(requests) == 1
    assert AUDIT_MARKER not in requests[0].prompt
    assert len(result.iterations) == 1
    assert result.iterations[0].audit_performed is False
    assert result.committed is False
    assert all(item.done for item in result.dod_items)


def test_audit_prompt_includes_successful_test_output():
    dod = parse_definition_of_done("- [x] testy projdou")
    prompt = _build_audit_prompt(
        "cil",
        dod,
        " M feature.py",
        "python -m pytest -q",
        True,
        "324 passed, 4 skipped in 30.77s",
    )

    assert "324 passed, 4 skipped in 30.77s" in prompt


def test_audit_prompt_explains_controller_owned_final_gate():
    dod = parse_definition_of_done(
        "- [x] implementation\n- [ ] ai-orchestrator vydá accepted / rejected verdikt"
    )
    prompt = _build_audit_prompt("cil", dod, "(čisté)", "pytest -q", True, "10 passed")
    assert "Controller-owned final audit gate" in prompt
    assert "accepted=true" in prompt


def test_controller_owned_audit_gate_does_not_repeat_executor(tmp_path):
    requests = []

    def run_fn(request):
        requests.append(request)
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text=_audit_response(request),
            )
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "implemented"}',
        )

    result = run_autonomous_loop(
        run_id="controller-audit-gate",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done(
            "- [ ] implementace\n"
            "- [ ] plnÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡ testovacÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­ sada projde a ai-orchestrator vydÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡ accepted/rejected verdikt"
        ),
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert len(requests) == 2
    assert result.iterations[0].requested_indices == [0]
    assert all(item.done for item in result.dod_items)


def test_czech_independent_audit_gate_is_controller_owned():
    items = parse_definition_of_done(
        "- [ ] implementace\n- [ ] plné testy a nezávislý audit projdou"
    )

    assert _controller_audit_gate_indices(items) == {1}


def test_controller_finalization_is_extracted_only_for_audit_specs():
    proof = {"status": "completed", "done": True, "commit_hash": "abc"}
    spec = json.dumps({"mode": "audit", "checkpoint": {"finalization": proof}})

    assert controller_finalization_from_spec(spec) == proof
    assert controller_finalization_from_spec(spec.replace('"audit"', '"autonomous"')) is None


def test_controller_finalization_audit_skips_provider_when_proof_is_current(git_repo, tmp_path):
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=git_repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "HEAD"], cwd=git_repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    proof = {
        "status": "completed", "done": True, "committed": False,
        "clean": True, "tests_passed": True, "pushed": True,
        "commit_hash": head, "remote_commit": head,
        "branch": branch, "remote": str(remote),
    }
    calls = []

    def provider_must_not_run(request):
        calls.append(request)
        raise AssertionError("controller audit must not call a provider")

    result = run_autonomous_loop(
        run_id="controller-finalization-audit",
        project_path=git_repo,
        goal="audit",
        dod_items=parse_definition_of_done("- [x] hotovo"),
        config=Config(),
        agent=FakeAgent(provider_must_not_run),
        logger=LOGGER,
        test_command='python -c "print(123)"',
        max_iterations=1,
        auto_commit_requested=False,
        controller_finalization=proof,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.iterations[0].audit_performed is True
    assert result.iterations[0].audit_rejected_indices == []
    assert calls == []


# -- Definition of Done parsing ----------------------------------------------


def test_parse_definition_of_done_checklist():
    items = parse_definition_of_done("- [x] hotovo\n- [ ] nesplneno\n* [ ] dalsi bod")
    assert [i.text for i in items] == ["hotovo", "nesplneno", "dalsi bod"]
    assert items[0].done is True
    assert items[1].done is False
    assert items[2].done is False


def test_parse_definition_of_done_inline_trello_checklist():
    spec = (
        "CIL: Opravit orchestrator.\n\n"
        "DEFINITION OF DONE: [ ] jedna neplatna odpoved => max 1 repair "
        "[x] opakovany protocol_error nezpusobi 7-10 plnych iteraci "
        "[ ] moznost failoveru na jineho providera "
        "[ ] checkpoint zachovan"
    )
    items = parse_definition_of_done(spec)
    assert [i.text for i in items] == [
        "jedna neplatna odpoved => max 1 repair",
        "opakovany protocol_error nezpusobi 7-10 plnych iteraci",
        "moznost failoveru na jineho providera",
        "checkpoint zachovan",
    ]
    assert [i.done for i in items] == [False, True, False, False]


def test_parse_definition_of_done_deduplicates_goal_and_rendered_checklist():
    spec = (
        "## Goal\nDEFINITION OF DONE: [ ] bod A [ ] bod B\n\n"
        "## Definition of Done\n- [ ] bod A\n- [ ] bod B\n"
    )

    items = parse_definition_of_done(spec)

    assert [item.text for item in items] == ["bod A", "bod B"]
    assert [item.done for item in items] == [False, False]

def test_parse_definition_of_done_plain_goal_is_single_item():
    items = parse_definition_of_done("Aplikace umi prihlasit uzivatele")
    assert len(items) == 1
    assert items[0].text == "Aplikace umi prihlasit uzivatele"
    assert items[0].done is False


def test_parse_definition_of_done_ignores_headings_and_prose():
    # Regression for run 9cd54b5bf219 (station-agent): a real spec file
    # mixes "# Heading" / "## Section" markdown with "- [ ] ..." checklist
    # items (see dod-station-agent-v1.md). Headings/prose must never become
    # DoD items - a heading can never be "done", so if it were parsed as an
    # item the checklist could never be fully satisfied.
    spec = (
        "# Definition of Done - Station Agent v1 (prvni plne funkcni verze)\n"
        "\n"
        "## GUI\n"
        "- [ ] Lokalni webove GUI dostupne na 127.0.0.1\n"
        "- [x] Server se vaze vyhradne na loopback\n"
        "\n"
        "## Testy\n"
        "Popisny text, ktery neni checklist a nesmi se stat polozkou.\n"
        "- [ ] Vsechny testy projdou bez chyby\n"
        "* [x] Bezpecnostni testy PTT/TX existuji\n"
    )
    items = parse_definition_of_done(spec)
    assert [i.text for i in items] == [
        "Lokalni webove GUI dostupne na 127.0.0.1",
        "Server se vaze vyhradne na loopback",
        "Vsechny testy projdou bez chyby",
        "Bezpecnostni testy PTT/TX existuji",
    ]
    assert [i.done for i in items] == [False, True, False, True]


def test_parse_live_evidence_contract_and_external_result():
    spec = (
        '- [x] Produkcni health check '
        '<!-- LIVE-EVIDENCE: {"command":"GET /health","expect":"ok"} -->\n'
        '<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"status: ok"} -->'
    )
    item = parse_definition_of_done(spec)[0]
    assert item.text == "Produkcni health check"
    assert item.live_command == "GET /health"
    assert item.live_expected == "ok"
    assert item.done is True
    assert item.live_evidence["passed"] is True


def test_live_item_without_or_with_failed_evidence_stays_open():
    declaration = (
        '- [x] Produkcni label '
        '<!-- LIVE-EVIDENCE: {"command":"read Trello labels","expect":"project_key"} -->'
    )
    missing = parse_definition_of_done(declaration)[0]
    assert missing.done is False
    assert missing.live_evidence is None

    failed = parse_definition_of_done(
        declaration + '\n<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"no labels"} -->'
    )[0]
    assert failed.done is False
    assert failed.live_evidence["passed"] is False


def test_local_tests_and_agent_claim_cannot_close_live_item(tmp_path):
    dod = parse_definition_of_done(
        '- [ ] Produkcni stav '
        '<!-- LIVE-EVIDENCE: {"command":"read production","expect":"ready"} -->'
    )

    def executor(request):
        return AgentRunResult(
            success=True,
            output_text='{"items":[{"index":0,"done":true}],"notes":"lokalne hotovo"}',
        )

    result = run_autonomous_loop(
        run_id="live-gate-fail", project_path=tmp_path, goal="over produkci",
        dod_items=dod, config=Config(), agent=FakeAgent(executor), logger=LOGGER,
        test_command='python -c "print(123)"', max_iterations=1,
        auto_commit_requested=False,
    )
    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert result.iterations[0].tests_passed is True
    assert result.dod_items[0].done is False
    assert result.iterations[0].audit_performed is False


def test_run_autonomous_completes_when_live_evidence_satisfied(git_repo):
    # Mirror-image regression of test_local_tests_and_agent_claim_cannot_close_live_item:
    # once AI Project Manager (or an operator) has performed the declared
    # read-only check and recorded a passing LIVE-RESULT *before* this run
    # even starts, parse_definition_of_done already marks the item done, so
    # the run must not stay stuck open forever - it should skip straight to
    # test+audit verification (no unmet indices to send an executor prompt
    # about) and reach COMPLETED, same as any other satisfied DoD item.
    dod = parse_definition_of_done(
        '- [x] Produkcni stav '
        '<!-- LIVE-EVIDENCE: {"command":"read production","expect":"ready"} -->\n'
        '<!-- LIVE-RESULT: {"index":0,"exit_code":0,"output":"stav: ready"} -->'
    )
    assert dod[0].done is True
    assert dod[0].live_evidence["passed"] is True

    executor_calls = []

    def executor(request):
        executor_calls.append(request)
        return AgentRunResult(success=True, output_text='{"items":[],"notes":"nic k reseni"}')

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="live-gate-pass", project_path=git_repo, goal="over produkci",
        dod_items=dod, config=cfg, agent=FakeAgent(_confirming_audit_or(executor)), logger=LOGGER,
        test_command=None, max_iterations=1, auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.dod_items[0].done is True
    assert result.dod_items[0].live_evidence["passed"] is True
    assert result.iterations[0].audit_performed is True
    # Nothing was unmet, so the (expensive) executor prompt must have been
    # skipped entirely - only the independent audit call was made.
    assert executor_calls == []


# -- success: DoD splnĂ„â€šĂ˘â‚¬ĹľÄ‚ËĂ˘â€šÂ¬ÄąĹşnÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡, testy proÄ‚â€žĂ„â€¦Ä‚â€ąĂ˘â‚¬Ë‡ly -> completed + commit -----------------


def test_run_autonomous_completed_with_commit(git_repo):
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        # Zm?na vznik? a? B?HEM autonomous b?hu, tak?e ji auto-commit
        # sm? bezpe?n? zahrnout.
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is True
    assert result.commit_hash
    assert len(result.iterations) == 1
    assert result.iterations[0].audit_performed is True
    assert all(item.done for item in result.dod_items)


def test_run_autonomous_does_not_commit_preexisting_dirty_tree(git_repo):
    (git_repo / "preexisting.txt").write_text("rozpracovana prace\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Over stav")

    def executor(request):
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="dirty-tree-test",
        project_path=git_repo,
        goal="Over stav",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is False
    assert result.commit_hash is None
    assert (git_repo / "preexisting.txt").exists()


def test_run_autonomous_accepts_done_claim_with_no_new_changes_on_preexisting_dirty_tree(git_repo):
    """Incident: card P5.20 (Station Agent - oprava P5, 2026-09-03). A
    checkpoint-resumed run starts from an already-dirty tree containing real,
    previously-verified implementation work from an earlier iteration/run
    that could never be committed (--no-commit is always on for this
    dispatch, see AGENTS.md rule 11). When the CURRENT run's executor
    correctly finds nothing left to change - because the work is already
    complete - the checkout's status is identical to this run's own start,
    which used to be indistinguishable from "the agent lied about being
    done" and reopened the item every single time (8+ times in production).
    The status-unchanged reject must only fire for a run that started
    CLEAN, where any diff unambiguously proves real work; a preexisting-dirty
    start must rely on tests_passed + the independent audit instead."""
    (git_repo / "preexisting.txt").write_text("uz hotova prace z drivejsi iterace\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Over stav")

    def executor(request):
        # Deliberately makes no further changes - the fix already exists in
        # preexisting.txt from a previous (unrepresented, never-committed)
        # iteration of this same checkpointed task.
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "jiz hotovo"}',
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="resumed-dirty-tree-test",
        project_path=git_repo,
        goal="Over stav",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.dod_items[0].done is True
    assert "checkout se od začátku běhu nezměnil" not in (result.iterations[0].note or "")
    assert len(result.iterations) == 1


def test_run_autonomous_accumulates_breaker_saved_attempts_across_calls(git_repo):
    # The PreToolUse circuit breaker (orchestrator/hooks/test_command_guard.py)
    # reports how many repeated test-invocation attempts it short-circuited
    # via AgentRunResult.breaker_saved_attempts - run_autonomous_loop must
    # sum this across every agent.run() call (executor + audit here) onto
    # the final AutonomousResult, same as it does for DoD/test tracking.
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text=_audit_response(request),
                breaker_saved_attempts=5,
            )
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
            breaker_saved_attempts=14,
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.breaker_saved_attempts == 19


def test_run_autonomous_completed_without_commit_when_run_did_not_request_it(git_repo):
    # The run itself did not request a commit (auto_commit_requested=False,
    # e.g. no --commit flag / auto_commit param passed) and the global config
    # default is also off - the ban on unsolicited commits must hold.
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}]}',
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is False
    assert result.commit_hash is None


def test_run_autonomous_commits_when_run_explicitly_requests_it_even_if_config_default_is_off(git_repo):
    # Regression: auto_commit_requested (an explicit per-run approval, e.g.
    # via the CLI --commit flag or the API auto_commit param) used to be
    # ANDed with config.git.auto_commit in _commit_if_ready, so an explicitly
    # approved run could never actually commit unless the global config
    # default was ALSO flipped on (which would blanket-approve every future
    # run, not just this explicitly approved one). An explicit per-run
    # approval must be sufficient on its own once the Definition of Done is
    # met, tests pass, and the audit does not reject anything.
    #
    # The change must happen INSIDE the executor callback, not before
    # run_autonomous_loop() is called - preexisting_dirty is computed from
    # the actual working tree at the very start of the loop (see
    # run_autonomous_loop), so writing the file any earlier would make the
    # tree dirty before the run even starts and _commit_if_ready would
    # (correctly) refuse to commit, defeating the point of this test.
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}]}',
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is True
    assert result.commit_hash


# -- failed tests: never commits, keeps trying until max_iterations ----------


def test_run_autonomous_never_commits_when_tests_fail(tmp_path):
    dod = parse_definition_of_done("- [ ] Neco co agent tvrdi ze je hotove")
    test_cmd = 'python -c "import sys; sys.exit(1)"'
    requests = []

    def run_fn(request):
        requests.append(request)
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "tvrdim ze hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=test_cmd,
        max_iterations=2,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert result.committed is False
    assert result.commit_hash is None
    assert len(result.iterations) == 2
    assert all(it.tests_passed is False for it in result.iterations)
    assert len(requests) == 2
    assert "Výsledek testů z minulé iterace" in requests[1].prompt
    assert "SELHALY" in requests[1].prompt


# -- max iterations: never fully completes, stops at the requested cap ------


def test_run_autonomous_stops_at_max_iterations(tmp_path):
    # A well-behaved executor that always covers every index it was ever
    # asked about (see DOD_BATCH_SIZE contract in _apply_dod_updates) -
    # marks exactly one additional item done per call, one at a time.
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(4)))
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        done_count = calls["n"]
        items = [{"index": idx, "done": idx < done_count} for idx in range(4)]
        return AgentRunResult(success=True, output_text=json.dumps({"items": items, "notes": f"iterace {done_count}"}))

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej 4 veci",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result.iterations) == 3
    assert sum(1 for item in result.dod_items if item.done) == 3
    assert result.committed is False


def test_run_autonomous_hard_cap_on_max_iterations(tmp_path):
    # even a huge --max-iterations can never exceed ABSOLUTE_MAX_ITERATIONS
    total_items = ABSOLUTE_MAX_ITERATIONS + 5
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(total_items)))
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        done_count = calls["n"]
        items = [{"index": idx, "done": idx < done_count} for idx in range(total_items)]
        return AgentRunResult(success=True, output_text=json.dumps({"items": items}))

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej hodne veci",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=100_000,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result.iterations) == ABSOLUTE_MAX_ITERATIONS


# -- no progress: stops early as blocked, before max_iterations --------------


def test_run_autonomous_detects_no_progress_and_blocks(tmp_path):
    dod = parse_definition_of_done("- [ ] nikdy se nesplni")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "porad zaseklo"}',
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="nemozny cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=10,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.BLOCKED
    assert len(result.iterations) == NO_PROGRESS_LIMIT
    assert len(result.iterations) < 10


# -- tests_passed must always be a real bool when a test command runs -------


def test_run_autonomous_tests_passed_is_always_bool(tmp_path):
    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    cfg = Config()
    for test_cmd, expected in [
        ('python -c "import sys; sys.exit(0)"', True),
        ('python -c "import sys; sys.exit(1)"', False),
    ]:
        result = run_autonomous_loop(
            run_id="test-run",
            project_path=tmp_path,
            goal="cil",
            dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
            config=cfg,
            agent=FakeAgent(run_fn),
            logger=LOGGER,
            test_command=test_cmd,
            max_iterations=1,
            auto_commit_requested=False,
        )
        assert len(result.iterations) == 1
        assert isinstance(result.iterations[0].tests_passed, bool)
        assert result.iterations[0].tests_passed is expected


# -- DoD evaluation: monotonic merge, protocol errors don't fake no-progress -


def test_run_autonomous_preserves_verified_done_items_across_iterations(tmp_path):
    """Requirement: the orchestrator must not blindly trust the agent's JSON
    claim each iteration - once an item is verified done, a later (possibly
    sloppier) agent response claiming it is no longer done must not un-mark
    it."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")
    calls = {"n": 0}

    def run_fn(request):
        calls["n"] += 1
        if calls["n"] == 1:
            payload = {"items": [{"index": 0, "done": True}, {"index": 1, "done": False}], "notes": "A hotovo"}
        else:
            # Second response incorrectly claims item 0 regressed to not-done.
            payload = {"items": [{"index": 0, "done": False}, {"index": 1, "done": False}], "notes": "pokus 2"}
        return AgentRunResult(success=True, output_text=json.dumps(payload))

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=False,
    )

    assert result.dod_items[0].done is True
    assert result.dod_items[1].done is False


def test_run_autonomous_malformed_response_does_not_cause_false_blocked(tmp_path):
    """Regression for run 9cd54b5bf219: an agent whose response cannot be
    parsed as the expected JSON contract must be recorded as a protocol
    error and given another (single, cheap-repair) chance, not silently
    treated as a repeated 'no progress' state that trips the blocked
    detector. It must also not be allowed to repeat forever (see
    PROTOCOL_ERROR_STREAK_LIMIT / run 7fffd21835174d9fb9a29237c897f6d2) - a
    plain single agent with no failover has nothing to fail over to, so the
    run stops as PROTOCOL_ERROR at the low threshold, well before
    max_iterations=3 would otherwise be reached."""
    dod = parse_definition_of_done("- [ ] bod, ktery se nikdy neoznaci")

    def run_fn(request):
        return AgentRunResult(success=True, output_text="tohle vubec neni JSON")

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert len(result.iterations) == PROTOCOL_ERROR_STREAK_LIMIT
    assert all(it.protocol_error for it in result.iterations)
    assert result.protocol_error_total == PROTOCOL_ERROR_STREAK_LIMIT


def test_run_autonomous_incomplete_json_is_protocol_error_not_no_progress(tmp_path):
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")

    def run_fn(request):
        # Only reports one of the two required items every time.
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "neuplne"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert len(result.iterations) == PROTOCOL_ERROR_STREAK_LIMIT
    assert all(it.protocol_error for it in result.iterations)


def test_run_autonomous_stops_well_before_max_iterations_on_repeated_protocol_error(tmp_path):
    """Regression for the production incident of 2026-08-26 (run
    7fffd21835174d9fb9a29237c897f6d2): Codex made real changes and passed
    tests in iterations 1-7, but never once returned the required DoD JSON,
    and the single repair reprompt also failed every time - so the run kept
    starting fresh full implementation iterations against an unchanged DoD
    until it burned through the whole provider usage limit. An agent that
    returns 7 consecutive invalid JSON responses (never valid, never a
    successful repair) must now cause the run to stop at
    PROTOCOL_ERROR_STREAK_LIMIT, not after all 7."""
    dod = parse_definition_of_done("- [ ] bod, ktery se nikdy neoznaci")
    calls = {"count": 0}

    def run_fn(request):
        calls["count"] += 1
        return AgentRunResult(success=True, output_text="tohle vubec neni JSON, porad dokola")

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=10,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert len(result.iterations) == PROTOCOL_ERROR_STREAK_LIMIT
    assert len(result.iterations) < 7
    # Each iteration with an unresolved protocol error makes exactly 2 agent
    # calls (main prompt + one cheap repair reprompt) - never a fresh full
    # implementation iteration on top of that.
    assert calls["count"] == PROTOCOL_ERROR_STREAK_LIMIT * 2
    assert result.protocol_error_total == PROTOCOL_ERROR_STREAK_LIMIT
    assert result.protocol_error_wasted_prompt_chars > 0
    assert "protokol" in (result.error or "").lower()


def test_run_autonomous_protocol_error_stop_notifies_slack_with_reason_and_waste(tmp_path, monkeypatch):
    """DoD point: when a run stops as PROTOCOL_ERROR (no failover-capable
    agent available), Slack/outbox must get a clear reason and a waste
    metric, not just a bare status code - see the module docstring and the
    production incident referenced above."""
    sent = []
    monkeypatch.setattr("orchestrator.autonomous.notify", lambda msg: sent.append(msg))

    dod = parse_definition_of_done("- [ ] bod, ktery se nikdy neoznaci")

    def run_fn(request):
        return AgentRunResult(success=True, output_text="tohle vubec neni JSON, porad dokola")

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run-notify",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=10,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert len(sent) == 1
    message = sent[0]
    assert "test-run-notify" in message
    assert "PROTOCOL_ERROR" in message
    assert "protokolovou nekompatibilitu" in message
    assert f"{PROTOCOL_ERROR_STREAK_LIMIT}x" in message
    assert "promarněná spotřeba" in message
    assert f"{result.protocol_error_total} protokolově chybných iterací" in message


def test_run_autonomous_fails_over_to_next_provider_on_repeated_protocol_error(tmp_path):
    """When the caller supplies a failover-capable agent (see
    agents/failover.py), a repeated protocol violation must trigger a
    failover to the next configured provider instead of stopping the whole
    run - the same right a quota/rate limit already gets."""
    dod = parse_definition_of_done("- [ ] Priprav feature")

    class FakeFailoverAgent(Agent):
        name = "fake-failover"

        def __init__(self):
            self.failover_calls: list[str] = []
            self._switched = False

        def is_available(self):
            return True, "ok"

        def force_failover_on_protocol_error(self, reason: str) -> bool:
            self.failover_calls.append(reason)
            self._switched = True
            return True

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            if not self._switched:
                return AgentRunResult(success=True, output_text="porad neplatny JSON")
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo po failoveru"}',
            )

    agent = FakeFailoverAgent()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=Config(),
        agent=agent,
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert len(agent.failover_calls) == 1
    assert result.status == AutonomousStatus.COMPLETED
    assert all(item.done for item in result.dod_items)


def test_run_autonomous_stops_as_budget_exceeded_when_no_failover(tmp_path):
    """Per-job, provider-specific financial hard cap (see AGENTS.md rule 9a
    and ARCHITECTURE.md "Per-job finanĂ„â€šĂ˘â‚¬ĹľĂ„Ä…Ă‚Â¤nÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­ limit"): independent of
    max_iterations, once this run's own cumulative reported cost_usd for
    the active provider exceeds its configured max_budget_usd, a plain
    (non-failover) agent must stop the run as BUDGET_EXCEEDED instead of
    continuing to spend past the cap."""
    dod = parse_definition_of_done("- [ ] bod, ktery se nikdy neoznaci")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "pracuji"}',
            cost_usd=2.0,
        )

    class NamedFakeAgent(FakeAgent):
        name = "claude-code"

    cfg = Config()
    cfg.claude_code.max_budget_usd = 1.0
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=NamedFakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.BUDGET_EXCEEDED
    assert len(result.iterations) == 1
    assert "finanční limit" in (result.error or "")


def test_run_autonomous_does_not_trigger_budget_cap_when_unconfigured(tmp_path):
    """None (the default) means no limit - reported spend must never be
    compared against a cap that was never configured."""
    dod = parse_definition_of_done("- [ ] bod")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
            cost_usd=999.0,
        )

    class NamedFakeAgent(FakeAgent):
        name = "claude-code"

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=Config(),
        agent=NamedFakeAgent(_confirming_audit_or(run_fn)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED


def test_run_autonomous_fails_over_to_next_provider_on_budget_exceeded(tmp_path):
    """When the caller supplies a failover-capable agent (see
    agents/failover.py), exceeding the active provider's configured
    max_budget_usd must trigger a failover to the next configured provider
    instead of stopping the whole run - the same right a repeated protocol
    violation or a quota/rate limit already gets."""
    dod = parse_definition_of_done("- [ ] Priprav feature")

    class FakeFailoverAgent(Agent):
        name = "fake-failover"

        def __init__(self):
            self.failover_calls: list[str] = []
            self._switched = False
            self.active_provider_name = "claude-code"

        def is_available(self):
            return True, "ok"

        def force_failover_on_budget_exceeded(self, reason: str) -> bool:
            self.failover_calls.append(reason)
            self._switched = True
            self.active_provider_name = "antigravity"
            return True

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            if not self._switched:
                return AgentRunResult(
                    success=True,
                    output_text='{"items": [{"index": 0, "done": false}], "notes": "prilis drahe"}',
                    cost_usd=5.0,
                )
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo po failoveru"}',
            )

    agent = FakeFailoverAgent()
    cfg = Config()
    cfg.claude_code.max_budget_usd = 1.0
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=agent,
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert len(agent.failover_calls) == 1
    assert result.status == AutonomousStatus.COMPLETED
    assert all(item.done for item in result.dod_items)


def test_protocol_error_does_not_apply_partial_dod_updates(tmp_path):
    """A protocol-invalid response must not mutate any DoD item."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text=json.dumps({
                "items": [
                    {"index": 0, "done": True},
                    {"index": 1, "done": True},
                    {"index": 2, "done": True},
                ],
                "notes": "obsahuje neplatny index",
            }),
        )

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert result.iterations[0].protocol_error is True
    assert all(item.done is False for item in result.dod_items)


# -- test command auto-detection: autonomous mode must not silently skip ----
# -- running tests just because none was explicitly configured -------------


def test_run_autonomous_autodetects_pytest_when_no_test_command_configured(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,  # not configured - must be auto-detected, not skipped
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations) == 1
    assert isinstance(result.iterations[0].tests_passed, bool)
    assert result.iterations[0].test_output is not None


def test_run_autonomous_autodetects_pytest_for_requirements_txt_project(tmp_path):
    """Regression: this repo (ai-orchestrator) itself has a "tests/" dir and
    a requirements.txt but no pyproject.toml/setup.py/setup.cfg/pytest.ini/
    tox.ini - before requirements.txt was added to _PYTHON_PROJECT_MARKERS,
    autonomous runs against this project's own repo never auto-detected a
    test command, so tests_passed stayed None forever instead of a real,
    orchestrator-verified pass/fail (see AGENTS.md rule 9)."""
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,  # not configured - must be auto-detected, not skipped
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations) == 1
    assert isinstance(result.iterations[0].tests_passed, bool)
    assert result.iterations[0].test_output is not None


def test_run_autonomous_autodetects_pytest_for_root_level_test_file(tmp_path):
    """Generated Inbox checkouts may place their first test beside the code."""
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "test_inbox_import.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    result = run_autonomous_loop(
        run_id="root-test-file",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert isinstance(result.iterations[0].tests_passed, bool)
    assert result.iterations[0].test_output is not None


def test_run_autonomous_does_not_autodetect_for_non_python_project(tmp_path):
    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,  # no pyproject.toml, no tests/ dir
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco"),
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations) == 1
    assert result.iterations[0].tests_passed is None


# -- agent error: stops immediately, does not keep retrying blindly ----------


def test_run_autonomous_agent_error_stops_immediately(tmp_path):
    dod = parse_definition_of_done("- [ ] neco")

    def run_fn(request):
        return AgentRunResult(success=False, output_text="", error="boom")

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.ERROR
    assert result.error == "boom"
    assert len(result.iterations) == 1


# -- _extract_json: robust against trailing/leading text and fences ---------


def test_extract_json_recovers_from_trailing_permission_denial_note():
    """Regression for run 11b4aaae08b4: the Claude Code CLI wrapper used to
    append a plain-text note *after* the agent's own valid JSON payload
    whenever a tool call was permission-denied, e.g.:
        '{"items": [...], "notes": "..."}\\n\\n[orchestrator] Claude odmÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­tl 15 akci(Ä‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­) kvÄ‚â€žĂ„â€¦Ă„Ä…Ă‚Â»li oprÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡vnĂ„â€šĂ˘â‚¬ĹľÄ‚ËĂ˘â€šÂ¬ÄąĹşnÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­m.'
    A naive `json.loads` on the whole string fails on the trailing text,
    which is exactly why 8 iterations in that run were misreported as
    protocol_error despite tests_passed=True and a perfectly valid agent
    response. `_extract_json` must recover the JSON object regardless."""
    text = (
        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], "notes": "hotovo"}'
        "\n\n[orchestrator] Claude odmÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­tl 15 akci(Ä‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­) kvÄ‚â€žĂ„â€¦Ă„Ä…Ă‚Â»li oprÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡vnĂ„â€šĂ˘â‚¬ĹľÄ‚ËĂ˘â€šÂ¬ÄąĹşnÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­m."
    )
    parsed = _extract_json(text)
    assert parsed is not None
    assert parsed["items"] == [{"index": 0, "done": True}, {"index": 1, "done": False}]
    assert parsed["notes"] == "hotovo"


def test_extract_json_recovers_from_leading_prose_and_fence():
    text = (
        "ShrnutÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€šĂ‚Â­ prÄ‚â€žĂ˘â‚¬ĹˇÄ‚â€ąĂ˘â‚¬Ë‡ce: opravil jsem X a Y.\n\n"
        "```json\n"
        '{"items": [{"index": 0, "done": true}], "notes": "ok"}\n'
        "```\n"
    )
    parsed = _extract_json(text)
    assert parsed == {"items": [{"index": 0, "done": True}], "notes": "ok"}


def test_extract_json_returns_none_for_genuinely_broken_json():
    assert _extract_json("tohle vubec neni JSON") is None
    assert _extract_json('{"items": [{"index": 0, "done": true premature cutoff') is None
    assert _extract_json("") is None


# -- batching: only a small batch of unmet items is requested per iteration -


def test_run_autonomous_requests_only_a_small_batch_not_the_whole_dod(tmp_path):
    """Regression for run 11b4aaae08b4 (66 DoD items): every iteration used
    to list and require a JSON entry for *all* DoD items, however many there
    were. That is both expensive (huge prompts every iteration) and fragile
    (one huge response is much more likely to get truncated/garbled). Each
    iteration must only ask about a small batch."""
    total_items = DOD_BATCH_SIZE * 3
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(total_items)))

    def executor(request):
        return AgentRunResult(success=True, output_text='{"items": [], "notes": "zadny pokrok"}')

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="hodne bodu",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations[0].requested_indices) == DOD_BATCH_SIZE
    assert result.iterations[0].requested_indices == list(range(DOD_BATCH_SIZE))
    # the prompt must not mention every single one of the total_items bodies
    assert result.iterations[0].prompt.count("bod ") <= DOD_BATCH_SIZE + 2


def test_run_autonomous_uses_one_dod_item_while_hermes_is_active(tmp_path):
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(3)))

    def executor(request):
        return AgentRunResult(success=True, output_text='{"items": [], "notes": "bez zmeny"}')

    hermes = FakeAgent(_confirming_audit_or(executor))
    hermes.name = "hermes"
    result = run_autonomous_loop(
        run_id="hermes-batch",
        project_path=tmp_path,
        goal="maly ukol",
        dod_items=dod,
        config=Config(),
        agent=hermes,
        logger=LOGGER,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations[0].requested_indices) == HERMES_DOD_BATCH_SIZE
    assert result.iterations[0].requested_indices == [0]


# -- cheap repair: malformed response gets one reprompt, not a fresh iteration


def test_run_autonomous_recovers_from_malformed_response_via_cheap_repair(tmp_path):
    """Regression for run 11b4aaae08b4: tests_passed=True on every iteration
    but the agent's raw response was unparsable, so every iteration was
    marked protocol_error and none of the already-verified DoD progress
    could ever lead to completion. The loop must (a) never reset a
    previously verified DoD item because of a later malformed response, and
    (b) recover via a single cheap repair reprompt rather than repeating a
    full, expensive implementation iteration."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            # iteration 1: clean valid response, marks bod A done
            return AgentRunResult(
                success=True,
                output_text=json.dumps(
                    {"items": [{"index": 0, "done": True}, {"index": 1, "done": False}], "notes": "A hotovo"}
                ),
            )
        if n == 2:
            # iteration 2, main call: genuinely broken/truncated JSON
            return AgentRunResult(success=True, output_text='{"items": [{"index": 1, "done": true premat')
        # iteration 2, cheap repair reprompt: valid JSON this time
        return AgentRunResult(
            success=True,
            output_text=json.dumps({"items": [{"index": 1, "done": True}], "notes": "B hotovo po repair"}),
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command='python -c "import sys; sys.exit(0)"',  # always tests_passed True
        max_iterations=4,
        auto_commit_requested=False,
    )

    # bod A (verified in iteration 1) must never be reset, even though
    # iteration 2 started with a malformed response.
    assert result.dod_items[0].done is True
    # bod B was recovered via the cheap repair reprompt in iteration 2.
    assert result.dod_items[1].done is True
    # every iteration's tests were independently verified by the orchestrator
    assert all(it.tests_passed is True for it in result.iterations)
    # iteration 2's malformed main response triggered exactly one repair
    # attempt, which succeeded - it must not be reported as a protocol error
    # (that would have falsely wiped out the no-progress tracking signal).
    assert len(result.iterations) == 2
    iter2 = result.iterations[1]
    assert iter2.repair_attempted is True
    assert iter2.repair_succeeded is True
    assert iter2.protocol_error is False
    # the run completed via the independent audit confirming the claim, not
    # by trusting the executor's self-report alone
    assert result.status == AutonomousStatus.COMPLETED
    assert iter2.audit_performed is True
    # cheap: only one extra repair call was needed, not a fresh full
    # implementation iteration (3 executor-side calls total: main x2 + 1 repair)
    assert calls["n"] == 3


# -- independent audit: rejects a false "done" claim before committing ------


def test_run_autonomous_audit_reopens_falsely_claimed_done_item(tmp_path):
    """The executor's self-report is never sufficient on its own (Manager/
    Executor/Auditor split, see AGENTS.md rule 8): if the independent audit
    pass finds a claimed-done item is not actually done, that item must be
    reopened (not committed), and the run must keep going instead of
    silently trusting the executor."""
    dod = parse_definition_of_done("- [ ] bod A")
    audit_calls = {"n": 0}

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                return AgentRunResult(
                    success=True,
                    output_text=_audit_response(request, accepted=False, index=0, evidence=f"{request.project_path.name}: bod A ve skutecnosti chybi"),
                )
            return AgentRunResult(success=True, output_text=_audit_response(request))
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.iterations[0].audit_rejected_indices == [0]
    assert result.iterations[0].audit_performed is True
    # not completed on the first iteration - the audit reopened it
    assert len(result.iterations) == 2
    assert result.dod_items[0].done is True


def test_audit_quality_fallback_rechecks_generic_refusal_with_next_provider(tmp_path):
    """A valid all-rejected refusal must not reopen implementation work.

    The orchestrator must obtain a real independent verdict from the next
    provider in the configured order; this is not an executor or PM verdict.
    """
    dod = parse_definition_of_done("- [ ] bod A")
    audit_calls = {"n": 0}

    class AuditFailoverFake(FakeAgent):
        name = "hermes"

        def __init__(self):
            super().__init__(self._run)
            self.active_provider_name = "hermes"
            self.failover_reasons = []

        def force_failover_on_audit_quality(self, reason):
            self.failover_reasons.append(reason)
            self.active_provider_name = "gemini"
            return True

        def _run(self, request):
            if AUDIT_MARKER not in request.prompt:
                return AgentRunResult(
                    success=True,
                    output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
                )
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                return AgentRunResult(
                    success=True,
                    output_text=_audit_response(
                        request,
                        accepted=False,
                        index=0,
                        evidence=(
                            "nelze samostatně potvrdit; audit nebyl proveden a "
                            "živé ověření nebylo provedeno"
                        ),
                    ),
                )
            return AgentRunResult(
                success=True,
                output_text=_audit_response(
                    request, evidence="module.py:10 a test_live_audit: potvrzeno"
                ),
            )

    agent = AuditFailoverFake()
    result = run_autonomous_loop(
        run_id="audit-quality-fallback",
        project_path=tmp_path,
        goal="audit",
        dod_items=dod,
        config=Config(),
        agent=agent,
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert audit_calls["n"] == 2
    assert agent.failover_reasons == [
        "audit vrátil zamítnutí všech bodů bez konkrétního ověření checkoutu"
    ]
    assert result.iterations[0].audit_rejected_indices == []


def test_audit_quality_detector_keeps_concrete_rejection_authoritative():
    assert _audit_needs_quality_fallback(
        [0], ["0:REJECT module.py:10 - chybí povinná validace"], 1
    ) is False


def test_audit_quality_detector_recognizes_agent_cannot_confirm_refusal():
    assert _audit_needs_quality_fallback(
        [0, 1],
        [
            "0:REJECT agent nemůže samostatně potvrdit auditní bod",
            "1:REJECT živé ověření nebylo provedeno",
        ],
        2,
    ) is True


def test_audit_quality_detector_recognizes_review_plan_without_verdict():
    assert _audit_needs_quality_fallback(
        [0, 1],
        [
            "0:REJECT needs verification - reading relevant files",
            "1:REJECT pending audit verdict",
        ],
        2,
    ) is True


def test_audit_evidence_must_name_the_project_scope():
    assert _audit_evidence_has_project_scope(
        "Diagnostikovat Station Agent a opravit jeho spuštění",
        Path("D:/orchestrator/station-agent"),
        ["0:OK tests/test_rig_safety.py: PTT guard je bezpečný"],
    ) is False
    assert _audit_evidence_has_project_scope(
        "Diagnostikovat Station Agent a opravit jeho spuštění",
        Path("D:/orchestrator/station-agent"),
        ["0:OK station_agent/cli.py: aplikace se spustí v mock režimu"],
    ) is True


def test_strict_audit_requires_evidence_for_every_dod_index(tmp_path):
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            # Missing index 1 must fail closed instead of silently accepting
            # every executor checkbox as verified completion.
            return AgentRunResult(
                success=True,
                output_text=(
                    '{"items":[{"index":0,"accepted":true,"method":"test: pytest test_a",'
                    '"evidence":"module.py:10 + test_a"}],"notes":"neuplne"}'
                ),
            )
        return AgentRunResult(
            success=True,
            output_text=(
                '{"items":[{"index":0,"done":true},{"index":1,"done":true}],'
                '"notes":"hotovo"}'
            ),
        )

    result = run_autonomous_loop(
        run_id="strict-audit-evidence",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert result.iterations[-1].audit_protocol_error is True
    assert "missing=[1]" in result.iterations[-1].note
    assert '"index":0' in result.iterations[-1].note


def test_audit_response_without_method_fails_closed():
    """The auditor must name *how* it verified each item, not just claim a
    verdict with prose evidence - an item without an explicit method is a
    protocol error, the same as one missing evidence entirely."""
    protocol_error, rejected, evidence_lines = _validate_audit_response(
        {"items": [{"index": 0, "accepted": True, "evidence": "module.py:10"}], "notes": "n"},
        {0},
        1,
    )
    assert protocol_error is True
    assert rejected == []


def test_audit_response_evidence_line_carries_method_and_verdict():
    """Per-DoD output must expose index + OK/REJECT + the concrete method
    used, not just a free-form evidence blob - this is what lets a caller
    (and Trello) see *how* each item was independently proven or refuted."""
    protocol_error, rejected, evidence_lines = _validate_audit_response(
        {
            "items": [
                {"index": 0, "accepted": True, "method": "runtime: spusteno `make run`", "evidence": "vypis OK"},
                {"index": 1, "accepted": False, "method": "artefakt: soubor chybi", "evidence": "config.yaml neexistuje"},
            ],
            "notes": "n",
        },
        {0, 1},
        2,
    )
    assert protocol_error is False
    assert rejected == [1]
    assert evidence_lines == [
        "0:OK [runtime: spusteno `make run`] vypis OK",
        "1:REJECT [artefakt: soubor chybi] config.yaml neexistuje",
    ]


def test_audit_prompt_instructs_category_specific_verification_method():
    """The auditor must derive its own verification method per DoD item
    based on the item's nature instead of applying one uniform check to
    every item (application -> runtime/live/E2E, artifact -> existence and
    content, integration/config/Git/CI -> actual current state)."""
    dod = parse_definition_of_done("- [ ] bod A")
    prompt = _build_audit_prompt("cil", dod, "(čisté)", None, None, None)

    assert "runtime" in prompt
    assert "end-to-end" in prompt or "E2E" in prompt
    assert "artefakt" in prompt
    assert "git" in prompt.lower()
    assert '"method"' in prompt
    assert "neověřitelný bod zůstává accepted=false" in prompt


def test_run_autonomous_waits_when_provider_is_limited(tmp_path):
    dod = parse_definition_of_done("- [ ] bod A")

    def run_fn(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="All providers LIMITED",
            limited=True,
            retry_after_seconds=600,
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="wait-test",
        project_path=tmp_path,
        goal="cekani na providera",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
    assert result.retry_after_seconds == 600
    assert result.error == "All providers LIMITED"
    assert result.dod_items[0].done is False


def test_run_autonomous_waits_when_provider_becomes_limited_during_repair(tmp_path):
    """Regression: a provider limit hit specifically on the cheap repair
    reprompt (not the main executor call) must still propagate as
    WAITING_FOR_PROVIDER, not be swallowed into a protocol_error iteration
    that eventually stops the run as PROTOCOL_ERROR (see incident
    cb501524e47e, 26.8.2026)."""
    dod = parse_definition_of_done("- [ ] bod A")
    calls = {"n": 0}

    def run_fn(request):
        assert AUDIT_MARKER not in request.prompt, "audit must not run when repair hits a provider limit"
        calls["n"] += 1
        if calls["n"] == 1:
            # Main executor call: malformed response triggers the repair path.
            return AgentRunResult(success=True, output_text="tohle vubec neni JSON")
        # Repair reprompt call: every provider is now exhausted.
        return AgentRunResult(
            success=False, output_text="", error="All providers LIMITED",
            limited=True, retry_after_seconds=450,
        )

    result = run_autonomous_loop(
        run_id="wait-during-repair",
        project_path=tmp_path,
        goal="cekani na providera behem repair",
        dod_items=dod,
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
    assert result.retry_after_seconds == 450
    assert result.error == "All providers LIMITED"
    assert result.dod_items[0].done is False
    assert len(result.iterations) == 1
    assert result.iterations[0].repair_attempted is True
    assert result.iterations[0].repair_succeeded is False
    assert calls["n"] == 2


def test_run_autonomous_waits_when_provider_becomes_limited_during_audit(tmp_path):
    """Regression: a provider limit hit specifically on the independent
    audit call (after the executor already claimed every DoD item done and
    the orchestrator's own tests agreed) must still propagate as
    WAITING_FOR_PROVIDER, not as an unresolved audit protocol error (see
    incident cb501524e47e, 26.8.2026)."""
    dod = parse_definition_of_done("- [ ] bod A")

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=False, output_text="", error="All providers LIMITED",
                limited=True, retry_after_seconds=300,
            )
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    result = run_autonomous_loop(
        run_id="wait-during-audit",
        project_path=tmp_path,
        goal="cekani na providera behem auditu",
        dod_items=dod,
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
    assert result.retry_after_seconds == 300
    assert result.error == "All providers LIMITED"
    # The executor's verified claim (item done) must survive - a provider
    # outage during the audit is not the same as the audit rejecting it.
    assert result.dod_items[0].done is True
    assert len(result.iterations) == 1
    assert result.iterations[0].audit_performed is True
    assert result.iterations[0].audit_protocol_error is True


def test_run_autonomous_aggregates_reported_usage_without_guessing_missing_values(tmp_path):
    calls = 0

    def run_fn(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "ok"}',
                input_tokens=100, output_tokens=20, total_tokens=120, cost_usd=0.01,
            )
        return AgentRunResult(
            success=True,
                output_text=_audit_response(request),
            input_tokens=30, output_tokens=5, total_tokens=35,
        )

    result = run_autonomous_loop(
        run_id="usage", project_path=tmp_path, goal="cil",
        dod_items=parse_definition_of_done("- [ ] bod"), config=Config(),
        agent=FakeAgent(run_fn), logger=LOGGER, test_command=None,
        max_iterations=1, auto_commit_requested=False,
    )

    # Both physical provider calls count: implementation and independent audit.
    assert result.usage_total["input_tokens"] == 130
    assert result.usage_total["output_tokens"] == 25
    assert result.usage_total["total_tokens"] == 155
    assert [event["stage"] for event in result.usage_events] == ["executor", "audit"]
    assert result.usage_total["thinking_tokens"] is None
    assert result.usage_total["source"] == "reported"

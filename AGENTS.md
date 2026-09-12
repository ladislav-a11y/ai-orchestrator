# AGENTS.md

Permanent rules for any AI agent (Claude Code, OpenAI Codex, or any future
provider) working inside this repository, whether editing it directly or
being driven by it as the orchestrated implementation agent. These rules
override generic defaults. If a user instruction conflicts with a rule in
this file, stop and ask - do not silently override safety rules.

## Non-negotiable safety rules

1. **Never pass `--dangerously-skip-permissions` or
   `--allow-dangerously-skip-permissions`** to the `claude` CLI, and never
   set `permission_mode: bypassPermissions`. This is enforced in code
   (`orchestrator/config.py`, `orchestrator/agents/claude_code.py`) - do not
   remove or weaken those checks. If a task seems to require it, that is a
   sign the task needs a human, not a workaround. The same rule applies to
   the `agy` (Antigravity) CLI: `orchestrator/agents/antigravity.py` never
   passes `--dangerously-skip-permissions` either, and always runs with
   `--mode accept-edits` (or another non-bypass mode from config) instead -
   verified against the real CLI to deny-by-default (not hang, not silently
   allow) anything that mode doesn't cover, such as shell commands. The same
   rule applies to the `codex` (OpenAI Codex) CLI:
   `orchestrator/agents/codex.py` never passes
   `--dangerously-bypass-approvals-and-sandbox` (or its `--yolo` alias) or
   `--dangerously-bypass-hook-trust`. For Codex CLI 0.149.0 the safe
   non-interactive contract depends on the configured mode: `read-only`
   uses `--sandbox read-only` without `--approve-for-me`, while
   `workspace-write` uses `--approve-for-me` without an explicit
   `--sandbox workspace-write`. These flags must not be combined.
   `danger-full-access` is never allowed, and Codex CLI 0.149.0 has no
   `--ask-for-approval` flag - do not reintroduce it.
2. **Never rewrite or delete Git history.** No `git reset --hard`, no
   `git rebase`, no `git filter-branch`, no `git push --force` /
   `--force-with-lease`, no deleting branches or tags. `orchestrator/git_utils.py`
   intentionally exposes no such operation - do not add one without an
   explicit, separate user request and a new confirmation step.
   `orchestrator/claude_settings.py` enforces the same list a second time, as
   explicit Claude Code `deny` rules written into every project's
   `.claude/settings.local.json` - keep both in sync if this list changes.
3. **Never push to a remote** in this phase of the project. Pushing is not
   implemented at all; adding it requires an explicit user decision (see
   ARCHITECTURE.md).
4. **Never create a commit when tests failed.** `runner.py`'s
   `_maybe_commit` refuses to commit if `task.tests_passed is False`.
   `autonomous.py`'s `_commit_if_ready` enforces the exact same rule a second
   time for the autonomous loop (only commits when every Definition of Done
   item is done AND the test command, if any, passed on that iteration). Do
   not route around either by calling `git commit` directly elsewhere.
   Related to rule 11: the implementation agent itself is never the one
   creating this commit.
5. **Never expose the local API beyond localhost.** `config.py` rejects any
   `api.host` other than `127.0.0.1` / `localhost` / `::1`. Do not add a
   flag or config path that binds to `0.0.0.0` or a public interface without
   the user explicitly asking for it and understanding the exposure.
6. **The user cannot program and does not use Git manually.** Every feature
   must work through `orchestrator.py <command>` with plain-language output.
   Do not require the user to run raw `git`/`pip`/`python -c` commands to use
   a normal feature - that belongs behind a CLI command or config option.
7. **Never silently delete user data.** `inbox/` files are moved to
   `inbox/processed/`, never deleted. Task history in `data/tasks.db` is
   never pruned automatically.
8. **Never let a project resolve outside `workspace_root`.** Every project
   path - whether a registry entry in `config.yaml` or a raw path passed to
   `--project` - is checked by `Config._ensure_within_workspace()`
   (`orchestrator/config.py`), once at config load time (registry entries)
   and again in `resolve_project()` (all callers). Default `workspace_root`
   is the parent directory of this repo (`D:\orchestrator`). Do not remove
   or weaken this check, and do not add a code path that builds a
   `project_path`/`cwd` for an agent without going through
   `resolve_project()`. For Antigravity (`agy`), the adapter explicitly
   passes `--add-dir <project_path>` in addition to subprocess cwd to mount
   the target project (empirical testing on Windows showed cwd alone defaults
   to scratch workspace), while `--sandbox` is omitted because it causes
   'context canceled' errors in Antigravity CLI on Windows.
9. **The autonomous loop (`orchestrator/autonomous.py`) must never run
   forever.** `run_autonomous_loop` hard-clamps `max_iterations` to
   `ABSOLUTE_MAX_ITERATIONS` regardless of what a caller/CLI flag requests,
   and stops (status `blocked`) after `NO_PROGRESS_LIMIT` consecutive
   *verified* iterations with an unchanged (unmet Definition-of-Done items,
   test result) signature - an iteration with a protocol error (unparsable/
   incomplete agent JSON, even after the one cheap repair reprompt), an
   unparsable independent-audit response, or a missing test result despite a
   configured test command does not count towards this (see
   `_apply_dod_updates` and ARCHITECTURE.md), so a confused agent gets a
   real chance to recover instead of being falsely declared stuck. Do not
   remove either cap, and do not add a "retry forever" or "ignore the cap"
   option.
9a. **The autonomous loop must also enforce a per-job, provider-specific
    financial hard cap, independent of the iteration cap above.**
    `ClaudeCodeAgentConfig`/`AntigravityAgentConfig`/
    `CodexAgentConfig.max_budget_usd` (`config.example.yaml`, `None` =
    unlimited) is checked once per iteration in `run_autonomous_loop`
    (`_provider_budget_usd`) against this run's own cumulative reported
    `cost_usd` for the currently active provider - never against a single
    call's cost, and never triggered by missing/unreported usage data
    (usage tracking is best-effort). When exceeded, the run fails over to
    the next configured provider if one is available
    (when the broker-backed facade exposes the corresponding failover hook;
    protocol failover is implemented by `BrokerBackedAgent`) or otherwise
    stops immediately with
    `AutonomousStatus.BUDGET_EXCEEDED` and a Slack notification - it must
    never keep spending past the configured cap "just this once". Do not
    remove this check and do not make it opt-out via a flag other than
    setting `max_budget_usd` to `None`.
10. **The autonomous loop must never let a malformed agent response burn a
    full extra implementation iteration, and must never trust the executor's
    own completion claim without independent verification.** A protocol
    error gets exactly one cheap repair reprompt (`_build_repair_prompt` -
    "resend just the JSON for these indices", never "try implementing this
    again") before being recorded as a protocol error. Before a commit is
    attempted, a separate audit pass (`_run_audit`) - a second prompt against
    the same `Agent`, explicitly instructed to verify only and never to
    implement - must confirm every claimed-done item; if it rejects any, they
    are reopened instead of committed. This is what run `11b4aaae08b4`
    lacked: 8 iterations with `tests_passed=True` were all misreported as
    `protocol_error` (see ARCHITECTURE.md for the root cause) with no cheap
    recovery path, burning a session's budget for zero recorded progress. Do
    not remove the repair step, do not let it turn into a second full
    iteration, and do not make the audit pass optional or skippable.
11. **The implementation agent must never create its own Git commit.**
    Committing only ever happens through the orchestrator's own Git layer
    (`git_utils.py`, called from `runner.py`'s `_maybe_commit` and
    `autonomous.py`'s `_commit_if_ready`), never as a side effect of the
    agent's own tool use, so it stays gated on the same verified-tests check
    as rule 4. For `claude-code` this is enforced twice: `claude_settings.py`
    denies `Bash(git commit:*)` in every project's
    `.claude/settings.local.json`, and `orchestrator/agents/claude_code.py`
    appends `NO_COMMIT_INSTRUCTION` to every prompt sent to the agent.
    `orchestrator/agents/antigravity.py` appends the same
    `NO_COMMIT_INSTRUCTION` text to every prompt it sends to `agy`; it has no
    equivalent to `claude_settings.py` because the real `agy` CLI has no
    project-local permission file to write into (its own permission config
    lives under the user's home directory, `~/.gemini/...`, keyed by an
    internally-managed project-ID mapping this orchestrator does not control
    - writing into that shared, undocumented, user-global file is out of
    scope and a materially bigger blast radius than a repo-local settings
    file). This is not a gap in practice: `AntigravityAgent` never passes
    `--dangerously-skip-permissions` (rule 1) and always runs with a
    non-bypass `--mode`, and that combination was verified against the real
    installed CLI to deny-by-default any Bash-equivalent tool call
    (`git commit` included) unless the user's own pre-existing global
    settings already allow-listed it - something entirely outside this
    orchestrator's control or knowledge either way, for any provider. `git
    status` and `git diff` stay allowed (the agent may need them to reason
    about its own changes). Keep the `claude-code` layers in sync if that
    rule ever changes; `orchestrator/agents/antigravity.py`'s module
    docstring documents the antigravity side of the same guarantee.
    `orchestrator/agents/codex.py` appends the same `NO_COMMIT_INSTRUCTION`
    text to every prompt it sends to `codex exec`; same rationale as
    Antigravity (no repo-local permission file this orchestrator controls),
    backstopped the same way by rule 1's sandbox/approval guarantee - a
    denied `git commit` from a sandboxed run comes back as an ordinary
    `error` event, not a hang or a silent allow.
11a. **Existing-state verification is not commit work.** A DoD item that
     checks the current HEAD, status, diff, remote, push evidence, or test
     sequence belongs to the independent audit phase and must explicitly say
     that no new commit is required. Only an explicit pending controller-
     finalization item may invoke the controller finalizer; otherwise the
     validator must not infer a new-commit requirement from evidence wording.
     When an audit rejects a card, its concrete feedback is persisted by the
     Project Manager and the next scheduler tick is the only retry boundary.
11b. **Independent audit execution is ai-orchestrator-only.** Agents may
     implement and provide evidence, while AI Project Manager may route and
     persist the result; neither may issue, infer, simulate, or replace the
     independent `accepted`/`rejected` verdict.
11c. **Test execution belongs to the orchestrator audit path.** A DoD item
     that asks to run regression tests or a test suite must be marked as
     `phase='audit'`; the implementation agent is not dispatched with such an
     item because the orchestrator runs and evaluates the configured test
     command after implementation. This prevents an agent from repeatedly
     leaving an impossible test item open and exhausting the iteration cap.
11d. **Production Gemini handoffs use the explicit free-tier-capable model and
     safe approval mode.** The adapter must invoke the installed `gemini` CLI
     headlessly with `--output-format json`, `--model gemini-2.5-flash`,
     `--approval-mode auto_edit`, and explicit `--skip-trust` for the selected
     workspace. `--skip-trust` is only a headless workspace acknowledgement;
     it is not a permission bypass. The adapter must never pass `--yolo`/`-y`
     or silently substitute another model. Malformed JSON, quota/rate errors,
     and timeouts must remain distinct `AgentRunResult` failures so the
     configured failover chain can continue without claiming successful work.
11e. **Groq is a strict free-only, project-scoped implementation provider.**
     `orchestrator/agents/groq.py` may use only `openai/gpt-oss-120b` while
     `groq.free_only` is enabled and must use the normal `on_demand` service
     tier - never `auto`, `flex`, performance tier, or an automatic model
     substitution. A 429/rate-limit response is `LIMITED` and must flow into
     the normal provider failover chain instead of retrying through a paid
     tier. Groq is an API model, not a coding CLI, so its implementation
     capability is supplied by a bounded local tool loop owned by the
     adapter. Those tools must remain confined to `project_path`, must reject
     path traversal and `.git` internals, and may only list/search/read/write
     UTF-8 project files plus read Git status/diff. There is no generic shell,
     test execution, commit, push, reset, rebase, deletion, or command
     execution tool. Structured final output is requested only after the tool
     loop finishes because Groq Structured Outputs and tool use are separate
     API phases. Tests and commits remain exclusively orchestrator-owned.

12. **A repeated protocol error must never be allowed to run indefinitely,
    even though it is excluded from the no-progress signature.** Rule 9
    correctly excludes a protocol error from `NO_PROGRESS_LIMIT` (an agent
    that garbles its output format is not the same as an agent that is
    stuck) - but that exclusion must not become a loophole. Incident run
    `7fffd21835174d9fb9a29237c897f6d2`: Codex made real changes and passed
    tests in iterations 1-7, but never once returned the required DoD JSON,
    the one cheap repair reprompt (rule 10) also failed every time, and the
    run kept starting brand-new full implementation iterations against an
    unchanged DoD until it burned through the whole provider usage limit.
    `run_autonomous_loop` tracks *consecutive* unresolved protocol errors
    separately (`PROTOCOL_ERROR_STREAK_LIMIT`, deliberately lower than
    `NO_PROGRESS_LIMIT`); once that streak is hit, it first tries
    `agent.force_failover_on_protocol_error()` (implemented by the v2
    `BrokerBackedAgent`; the broker, not the autonomous loop, chooses the
    replacement provider - a repeated protocol violation gets the same right
    to fail over to another configured provider as a quota/rate limit does),
    and only stops the run (status `AutonomousStatus.PROTOCOL_ERROR`) if no
    other provider is configured/available. Do not raise `PROTOCOL_ERROR_STREAK_LIMIT` to
    match `NO_PROGRESS_LIMIT`, and do not remove the failover attempt.
13. **`WAITING_FOR_PROVIDER` must never look or be reported like ordinary
    silent progress, and never leave a stale queue row behind.** Incident
    `cb501524e47e` (26.8.2026): every configured provider was LIMITED, the
    orchestrator correctly saved a waiting task, but AI Project Manager only
    saw a generic `in_progress`-looking state with no Slack message and no
    indication of whether anything would resume on its own. Three
    invariants keep this from recurring:
    - `OrchestratorService.run_autonomous` always looks up any existing
      WAITING_FOR_PROVIDER/RUNNING queue row for the same project+spec
      (`existing_waiting_task`, via `queue.find_active_autonomous`) and
      reconciles it - not just when the internal `_waiting_worker` passes
      `_waiting_task` explicitly. AI Project Manager drives this CLI as a
      brand-new one-shot process per invocation (see README ch.9), so a
      completed/blocked/errored re-run from a *different* process instance
      must still close out the row the previous invocation left waiting -
      never leave it stuck at `WAITING_FOR_PROVIDER` forever.
    - `OrchestratorService.auto_resume_active` (constructor `persistent`
      flag) must stay `False` for the CLI's plain `OrchestratorService()`
      (`autonomous`/`run`) and `True` only for the long-running
      `orchestrator.py api` process (see `api.py`'s `get_service()`) - it is
       what the CLI output and
      `outbox/autonomous-<run_id>.json`'s `auto_resume_active` field use to
      tell the caller whether anything will resume this run on its own.
      Never hardcode it to `True`: the CLI process exits right after
      printing the result, so its own waiting-worker thread never gets a
      chance to fire.
     - `WAITING_FOR_PROVIDER` is reported in the structured result, outbox
       and log with each provider's status and nearest known reset. The old
       orchestrator-level `slack_notify` path is retired. Slack is reserved
       for the provider-owned receipt emitted during an actual provider run;
       it must not be used as a second source of workflow state.
14. **Verifying the real Codex CLI contract against the live binary is a
    manual step - never something an autonomous iteration or the default
    test suite does on its own.** Two equivalent, read-only, file-change-free
    entry points exist for this, both forcing `--sandbox read-only`
    regardless of the configured default and both failing closed (never
    reporting success) if anything in the target directory changes:
    - `orchestrator.py doctor --live` (`doctor._check_codex_live` in
      doctor.py) - the same entry point already used for the equivalent
      Claude Code `doctor --live` login-verification step, now covering
      Codex too. This is the one to actually run by hand.
    - `tests/test_codex_agent.py::
      test_live_smoke_reads_project_state_without_changes` (mirrors the
      equivalent Antigravity live smoke test), gated behind
      `AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST=1` so it never runs under plain
      `pytest` (see the "Style and scope" rule below: tests must not call a
      live paid API by default) - useful in a CI job that isn't set up to
      call `orchestrator.py doctor`.
    Neither runs inside an autonomous dev iteration, which has neither
    permission to invoke a live provider CLI nor permission to run tests
    itself - test execution is exclusively the orchestrator's/a human's job.
    See README.md "Ověření reálného Codex CLI kontraktu" for both commands.
    `doctor._check_codex_live`'s own correctness (schema validation, and
    that a mutated directory is reported as a broken contract rather than
    silently accepted) is covered by ordinary, always-run unit tests in
    tests/test_doctor.py that fake `CodexAgent.run()` - only the real network
    call to the live binary itself is the part that needs a human/CI to
    actually trigger it once.
15. **Every operational step must contain one independent command and wait
    for its complete output before the next step.** Do not concatenate
    independent actions with `;`, `&&`, `||`, or a pipeline. If a command
    fails, stop and diagnose the new state instead of repeating it unchanged.
16. **Whitespace and encoding checks are mandatory and scoped.** Files
    changed by the current task must remain UTF-8 without BOM, LF, without
    trailing whitespace, and without an accidental extra blank line at EOF;
    `git --no-pager diff --check` must be clean for those files before a
    commit. If the check reports a pre-existing violation in an unrelated
    dirty file, record it as pre-existing and do not silently modify that
    file as part of the current task.
17. **Tests must use the target repository's real Windows interpreter.** When
    `<project>\\.venv\\Scripts\\python.exe` exists, invoke it by its absolute
    path and put its `Scripts` directory first on `PATH` for test subprocesses.
    Never treat the system `python` command or a WindowsApps alias as a valid
    project environment without verifying its resolved executable path; an
    alias failure can masquerade as an implementation regression before the
    tested code even runs.
    If `py_compile` or `pytest` hits `PermissionError`/`WinError 5` while
    writing the repository's `__pycache__` or pytest temp directory, do not
    repeat the unchanged command. First record the exact error, then rerun
    the same verification with `PYTHONPYCACHEPREFIX` or pytest `--basetemp`
    under the system temporary directory and clean that exact temporary path
    after verification.

## When adding a new agent/provider (e.g. OpenAI Codex)

- Implement `orchestrator/agents/base.py`'s `Agent` interface in a new file
  (`orchestrator/agents/codex.py`, `orchestrator/agents/gemini.py`, ...).
- Register it in `orchestrator/agents/registry.py` - do not scatter
  `if agent_name == "..."` branches elsewhere in the codebase.
- Apply the same non-interactive-safe defaults: no destructive-by-default
  flags, no auto-approval of dangerous operations, clear error messages
  when the CLI/API isn't installed or isn't authenticated.
- Do not implement the "review agent" stage as a special case bolted onto
  `runner.py`'s control flow for one specific provider - it should be just
  another `Agent`, invoked as an additional pipeline stage.

## Style and scope

- Keep changes minimal and scoped to what was asked. This project
  deliberately avoids speculative abstraction (see project root
  instructions) - do not add plugin systems, config formats, or provider
  integrations that were not requested.
- Config files are the only place secrets/paths should live. Never hardcode
  a user-specific path, API key, or token in source.
- Every new capability that touches the filesystem, Git, or a network
  socket needs a corresponding test under `tests/` using mocks/fakes, not a
  live call to a paid API.
- Write user-facing CLI output and documentation in Czech, matching the
  existing `README.md`/CLI messages, since the primary user is not a
  Czech-to-English translator. Code, comments, and this file stay in
  English, matching the rest of the codebase.

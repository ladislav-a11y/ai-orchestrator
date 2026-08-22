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
   allow) anything that mode doesn't cover, such as shell commands.
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
   `resolve_project()`.
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

## When adding a new agent/provider (e.g. OpenAI Codex)

- Implement `orchestrator/agents/base.py`'s `Agent` interface in a new file
  (`orchestrator/agents/codex.py`, ...).
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

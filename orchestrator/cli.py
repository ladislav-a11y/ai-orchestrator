"""Command-line entry point. See README.md for usage examples in Czech."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from orchestrator.autonomous import ABSOLUTE_MAX_ITERATIONS, AutonomousStatus, DEFAULT_MAX_ITERATIONS
from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.registry import build_agent
from orchestrator.config import (
    AVAILABLE_AGENTS,
    load_config,
    with_provider_model_overrides,
)
from orchestrator.doctor import run_doctor
from orchestrator.models import TaskStatus
from orchestrator.service import OrchestratorService
from orchestrator.context_compaction import require_planner_input


_INBOX_PLANNING_RECIPE_PATH = Path(__file__).with_name("inbox_planning_recipe.md")


def _load_inbox_planning_recipe() -> str:
    """Load the versioned AI planning recipe and fail closed if unavailable."""
    recipe = _INBOX_PLANNING_RECIPE_PATH.read_text(encoding="utf-8").strip()
    if not recipe:
        raise ValueError("Inbox planning recipe is empty")
    return recipe


def _positive_timeout_seconds(raw: str) -> float:
    """Parse a finite, strictly positive per-provider timeout."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("timeout musí být číslo") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout musí být konečné kladné číslo")
    return value


def _print_doctor(report) -> int:
    print("== ai-orchestrator doctor ==")
    for check in report.checks:
        mark = "[OK]" if check.ok else "[CHYBA]"
        print(f"{mark:8} {check.name}: {check.message}")
    print()
    if report.all_ok:
        print("Vše v pořádku.")
        return 0
    print("Některé kontroly selhaly - viz výše.")
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(live=args.live, live_project=args.live_project)
    return _print_doctor(report)


def cmd_run(args: argparse.Namespace) -> int:
    service = OrchestratorService()
    try:
        task = service.submit(
            project_ref=args.project,
            prompt=args.prompt,
            agent_name=args.agent,
            test_command_override=args.test_command,
            auto_commit=_resolve_auto_commit(args),
            source="cli",
        )
    except ValueError as e:
        print(f"Chyba: {e}")
        return 1

    print(f"Task {task.id} spuštěn na projektu '{task.project}' ({task.project_path})...")
    result_task = service.run_sync(task)
    return _print_task(result_task)


def _resolve_auto_commit(args: argparse.Namespace) -> "bool | None":
    """Resolve --commit/--no-commit into the `auto_commit` override passed to
    the service. argparse's mutually-exclusive group already refuses both
    flags at once, so at most one of `args.commit`/`args.no_commit` is ever
    True here. Neither flag given -> None, so the service falls back to
    `config.git.auto_commit` exactly as before - this only adds a way to
    explicitly opt a single invocation IN, without touching global config.yaml
    (see runner.py `_maybe_commit` / autonomous.py `_commit_if_ready`)."""
    if args.no_commit:
        return False
    if args.commit:
        return True
    return None


def _format_denial(denial) -> str:
    if isinstance(denial, dict):
        tool = denial.get("tool_name") or denial.get("tool") or denial.get("name") or "?"
        tool_input = denial.get("tool_input") or denial.get("input") or denial.get("parameters")
        return f"{tool}({tool_input})" if tool_input is not None else str(tool)
    return str(denial)


def _print_task(task) -> int:
    print(f"\n== Task {task.id} - {task.status.value} ==")
    if task.result:
        print("\n--- Výsledek agenta ---")
        print(task.result)
    if task.permission_denials:
        print(f"\n--- Zamítnuté akce kvůli oprávněním ({task.permission_denials}) ---")
        for denial in task.permission_denial_details:
            print(f"  - {_format_denial(denial)}")
    if task.test_command:
        print(f"\n--- Testy ({task.test_command}) ---")
        print(f"Prošly: {task.tests_passed}")
        if task.test_output and not task.tests_passed:
            print(task.test_output[-2000:])
    if task.committed:
        print(f"\nVytvořen commit: {task.commit_hash}")
    elif task.status == TaskStatus.DONE:
        print("\nCommit nebyl vytvořen (auto_commit vypnutý, žádné změny, nebo projekt není Git repozitář).")
    if task.error:
        print(f"\nChyba: {task.error}")
    print(f"\nCena (odhad): ${task.cost_usd or 0:.4f}")
    print(f"Log: logs/tasks/{task.id}.log  |  Výsledek: outbox/{task.id}.json")
    return 0 if task.status == TaskStatus.DONE else 1


def cmd_autonomous(args: argparse.Namespace) -> int:
    if args.max_iterations < 1:
        print("Chyba: --max-iterations musí být alespoň 1.")
        return 1
    if args.max_iterations > ABSOLUTE_MAX_ITERATIONS:
        print(
            f"Chyba: --max-iterations={args.max_iterations} přesahuje bezpečný strop "
            f"{ABSOLUTE_MAX_ITERATIONS}. Orchestrátor nesmí běžet neomezeně dlouho - "
            "zvol nižší hodnotu."
        )
        return 1

    spec_text = None
    if args.spec:
        spec_path = Path(args.spec)
        if not spec_path.exists():
            print(f"Chyba: --spec soubor '{spec_path}' neexistuje.")
            return 1
        spec_text = spec_path.read_text(encoding="utf-8")

    service = OrchestratorService()
    try:
        run_id, result = service.run_autonomous(
            project_ref=args.project,
            goal=args.goal,
            spec_text=spec_text,
            agent_name=args.agent,
            model_override=args.model,
            test_command_override=args.test_command,
            max_iterations=args.max_iterations,
            auto_commit=_resolve_auto_commit(args),
            implementation_only=args.implementation_only,
            run_id=args.run_id,
        )
    except ValueError as e:
        print(f"Chyba: {e}")
        return 1

    return _print_autonomous_result(run_id, result)


def _print_autonomous_result(run_id: str, result) -> int:
    # ``autonomous`` is a one-shot CLI command; its service does not keep a
    # persistent waiting worker alive after this function returns.
    auto_resume_active = False
    print(f"\n== Autonomní běh {run_id} - {result.status.value} ==")
    print(f"Iterací provedeno: {len(result.iterations)}")
    if result.restored_from_checkpoint:
        print(
            f"Obnoveno z checkpointu předchozího běhu: {result.restored_from_checkpoint} bod(ů) "
            "Definition of Done (běh nezačínal od bodu 0)."
        )
    print("\n--- Definition of Done ---")
    for i, item in enumerate(result.dod_items):
        mark = "[x]" if item.done else "[ ]"
        print(f"{mark} {i}. {item.text}")
        live_command = getattr(item, "live_command", None)
        if live_command is not None:
            evidence = getattr(item, "live_evidence", None)
            if isinstance(evidence, dict) and evidence.get("passed") is True:
                live_state = "OK (ověřeno živým důkazem)"
            elif isinstance(evidence, dict):
                live_state = "SELHALO - živý důkaz neodpovídá očekávanému výstupu"
            else:
                live_state = "CHYBÍ - čeká se na LIVE-RESULT z reálné integrace/produkce"
            print(f"    živý důkaz vyžadován: {live_command!r} -> {item.live_expected!r}; stav: {live_state}")

    if result.status == AutonomousStatus.WAITING_FOR_PROVIDER:
        if result.retry_after_seconds is not None:
            print(
                f"\nZastaveno: čekání na dostupného providera; další pokus nejdříve za "
                f"{result.retry_after_seconds:.0f} s."
            )
        else:
            print("\nZastaveno: čekání na dostupného providera; čas dalšího pokusu není znám.")
        # DoD (produkční incident cb501524e47e, 26.8.2026): výstup musí
        # jednoznačně říct, jestli se běh sám obnoví, nebo je nutný další
        # zásah - viz OrchestratorService.auto_resume_active.
        if auto_resume_active:
            print(
                "Automatické pokračování JE aktivní (trvalý worker tohoto procesu) - "
                "běh sám naváže z checkpointu, jakmile limit vyprší."
            )
        else:
            print(
                "Automatické pokračování NENÍ aktivní (jednorázový běh bez trvalého "
                "workeru/scheduleru) - je nutné po resetu spustit stejný příkaz znovu "
                f"(`orchestrator.py autonomous ... --run-id {run_id}`); naváže z uloženého "
                "checkpointu, ne od začátku."
            )
    elif result.status == AutonomousStatus.BLOCKED:
        print("\nZastaveno: stejný stav/chyba se opakuje bez pokroku (blocked).")
    elif result.status == AutonomousStatus.PROTOCOL_ERROR:
        print(
            "\nZastaveno: agent opakovaně nevrátil platný JSON kontrakt (protokolová chyba), "
            "i po repair pokusu a bez dalšího providera k dispozici."
        )
    elif result.status == AutonomousStatus.BUDGET_EXCEEDED:
        print(
            "\nZastaveno: aktivní provider překročil svůj nakonfigurovaný finanční limit pro "
            "tuto úlohu a žádný další provider nebyl k dispozici."
        )
    elif result.status == AutonomousStatus.MAX_ITERATIONS:
        print("\nZastaveno: dosažen maximální počet iterací, Definition of Done ještě není splněná.")
    elif result.status == AutonomousStatus.ERROR:
        print("\nZastaveno: agent selhal.")

    if result.committed:
        print(f"\nVytvořen commit: {result.commit_hash}")
    elif result.status == AutonomousStatus.COMPLETED:
        print(
            "\nCommit nebyl vytvořen (auto_commit vypnutý, žádné změny, nebo projekt "
            "není Git repozitář)."
        )
    if result.error:
        print(f"\nChyba: {result.error}")

    print(f"\nLog: logs/autonomous/{run_id}.log  |  Výsledek: outbox/autonomous-{run_id}.json")
    return 0 if result.status == AutonomousStatus.COMPLETED else 1


def cmd_status(args: argparse.Namespace) -> int:
    service = OrchestratorService()
    if args.task_id:
        task = service.get_task(args.task_id)
        if task is None:
            print(f"Task {args.task_id} nenalezen.")
            return 1
        return _print_task(task)

    status_filter = TaskStatus(args.filter) if args.filter else None
    tasks = service.list_tasks(status=status_filter, limit=args.limit)
    if not tasks:
        print("Žádné úkoly.")
        return 0
    print(f"{'ID':12} {'STAV':10} {'PROJEKT':20} {'VYTVOŘENO':22} ZADÁNÍ")
    for t in tasks:
        prompt_preview = t.prompt.replace("\n", " ")[:60]
        print(f"{t.id:12} {t.status.value:10} {t.project:20} {t.created_at:22} {prompt_preview}")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    from orchestrator.api import serve

    print("Spouštím lokální API (jen 127.0.0.1) - Ctrl+C pro ukončení...")
    serve()
    return 0


def cmd_import_inbox(args: argparse.Namespace) -> int:
    service = OrchestratorService()
    created = service.import_inbox()
    print(f"Naimportováno {len(created)} úkol(ů) z inbox/.")
    for t in created:
        print(f"  {t.id}  {t.project}  {t.prompt[:60]!r}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    service = OrchestratorService()
    if not service.config.projects:
        print("V config.yaml (sekce 'projects') není zaregistrovaný žádný projekt.")
        return 0
    for name, entry in service.config.projects.items():
        tc = f" [testy: {entry.test_command}]" if entry.test_command else ""
        print(f"{name}: {entry.path}{tc}")
    return 0


def _planning_usage(result, provider: str | None = None) -> dict:
    fields = ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "cost_usd")
    events = list(result.usage_events or [])
    if not events and any(
        getattr(result, field, None) is not None for field in fields
    ):
        events = [{
            "provider": provider or getattr(result, "model_source", None) or "unknown",
            "source": "reported",
            **{field: getattr(result, field, None) for field in fields},
        }]
    by_provider = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        provider = event.get("provider") or "unknown"
        bucket = by_provider.setdefault(provider, {field: None for field in fields})
        for field in fields:
            value = event.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[field] = (bucket[field] or 0) + value
        bucket["source"] = "reported"
    total = {field: None for field in fields}
    for bucket in by_provider.values():
        for field in fields:
            if bucket.get(field) is not None:
                total[field] = (total[field] or 0) + bucket[field]
    total["source"] = "reported" if by_provider else None
    return {"by_provider": by_provider, "total": total}


def cmd_plan_inbox(args: argparse.Namespace) -> int:
    """Run one read-only provider turn to structure a human Inbox request."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Inbox planner input must be a JSON object")
        # The Inbox source and project identities are canonical planning input,
        # not disposable history: preserve them completely or fail closed.
        require_planner_input(payload)
        recipe = _load_inbox_planning_recipe()
        config = load_config()
        central_broker = args.agent == "provider-broker"
        if central_broker and args.model:
            raise ValueError("--model nelze použít s centrálním provider-brokerem")
        # Planning receives an empty disposable workspace. The provider can
        # reason over the supplied card text but cannot mutate a project
        # checkout. Provider-specific read-only modes add a second guard.
        safe_config = replace(
            config,
            antigravity=replace(config.antigravity, mode="plan"),
            codex=replace(config.codex, sandbox_mode="read-only"),
        )
        if central_broker:
            if args.provider_timeout_seconds is not None:
                timeout = args.provider_timeout_seconds
                safe_config = replace(
                    safe_config,
                    claude_code=replace(
                        safe_config.claude_code, timeout_seconds=timeout
                    ),
                    antigravity=replace(
                        safe_config.antigravity, timeout_seconds=timeout
                    ),
                    codex=replace(safe_config.codex, timeout_seconds=timeout),
                    groq=replace(safe_config.groq, timeout_seconds=timeout),
                )
        elif args.provider_timeout_seconds is not None:
            raise ValueError("--provider-timeout-seconds vyžaduje provider-broker")
        provider_models = None
        safe_config = with_provider_model_overrides(safe_config, provider_models)
        if args.model:
            config_attr = {
                "antigravity": "antigravity",
                "claude-code": "claude_code",
                "codex": "codex",
                "groq": "groq",
            }[args.agent]
            safe_config = replace(
                safe_config,
                **{
                    config_attr: replace(
                        getattr(safe_config, config_attr), model=args.model
                    )
                },
            )
        agent = build_agent(args.agent, safe_config)
        prompt = (
            "Jsi AI planner pro Inbox AI Project Manageru. Neprováděj žádné změny "
            "souborů, nepoužívej git a nic neimplementuj. Následující verzovaný "
            "recept je závazný. Před vrácením výsledku proveď jeho vlastní "
            "kontrolní seznam a vrať pouze JSON odpovídající poskytnutému output "
            "schema.\n\n--- ZÁVAZNÝ RECEPT ---\n"
            + recipe
            + "\n--- KONEC RECEPTU ---\n\n--- VSTUP ---\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        with tempfile.TemporaryDirectory(prefix="ai-orchestrator-inbox-plan-") as workspace:
            result = agent.run(
                AgentRunRequest(
                    project_path=Path(workspace),
                    prompt=prompt,
                    failover_on_error=False,
                    output_schema={
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 32,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "project_key": {
                                            "anyOf": [
                                                {"type": "string", "minLength": 1},
                                                {"type": "null"},
                                            ]
                                        },
                                        "scope": {"type": "string", "minLength": 1},
                                        "task": {"type": "string", "minLength": 1},
                                        "next_step": {"type": "string", "minLength": 1},
                                        "priority": {"type": "number", "minimum": 0, "maximum": 5.999999},
                                        "priority_reason": {"type": "string", "minLength": 1},
                                        "work_type": {
                                            "type": "string",
                                            "enum": ["implementation", "research", "configuration", "integration", "tests"],
                                        },
                                        "split_reason": {"type": "string", "minLength": 1},
                                        "source_refs": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 16,
                                            "items": {"type": "string", "minLength": 1},
                                        },
                                        "verification": {
                                            "type": "object",
                                            "properties": {
                                                "required": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "maxItems": 7,
                                                    "items": {
                                                        "type": "string",
                                                        "enum": [
                                                            "static", "unit", "integration",
                                                            "regression", "runtime", "gui", "config",
                                                        ],
                                                    },
                                                },
                                                "acceptable": {
                                                    "type": "array",
                                                    "maxItems": 7,
                                                    "items": {
                                                        "type": "string",
                                                        "enum": [
                                                            "static", "unit", "integration",
                                                            "regression", "runtime", "gui", "config",
                                                        ],
                                                    },
                                                },
                                                "reason": {"type": "string", "minLength": 1},
                                            },
                                            "required": ["required", "acceptable", "reason"],
                                            "additionalProperties": False,
                                        },
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "integer", "minimum": 0, "maximum": 31},
                                        },
                                    },
                                    "required": [
                                        "project_key", "scope", "task", "next_step", "priority",
                                        "priority_reason", "work_type", "split_reason",
                                        "source_refs", "verification", "depends_on",
                                    ],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["tasks"],
                        "additionalProperties": False,
                    },
                )
            )
        status_snapshot = getattr(agent, "provider_status_snapshot", lambda: {})()
        if not isinstance(status_snapshot, dict):
            status_snapshot = {}
        active_provider = getattr(agent, "active_provider_name", None) or args.agent
        provider_sequence = list(status_snapshot) or [active_provider]
        active_config_attr = {
            "claude-code": "claude_code",
            "antigravity": "antigravity",
            "codex": "codex",
            "groq": "groq",
        }.get(active_provider)
        model_config = getattr(safe_config, active_config_attr, None)
        output = {
            "success": result.success,
            "provider": active_provider,
            "selected_provider": args.agent,
            "active_provider": active_provider,
            # Report the model the provider actually returned; only fall back
            # to the provider-specific configured value when the provider did
            # not expose a receipt model (e.g. a mocked/legacy CLI).
            "model": result.model or getattr(model_config, "model", None),
            "active_model": result.model or getattr(model_config, "model", None),
            "provider_models": provider_models or {},
            "provider_sequence": provider_sequence,
            "provider_statuses": status_snapshot,
            "selection_reason": result.selection_reason,
            "usage": _planning_usage(result, active_provider),
            "output": result.output_text,
            "error": result.error,
            "unavailable": result.unavailable,
            "limited": result.limited,
            "timed_out": result.timed_out,
        }
        print(json.dumps(output, ensure_ascii=False))
        return 0 if result.success else 1
    except Exception as exc:  # noqa: BLE001 - CLI must return a safe JSON error
        print(json.dumps({"success": False, "error": str(exc), "unavailable": False}, ensure_ascii=False))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator.py", description="Lokální AI orchestrátor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="Zkontroluje prostředí (Python, Git, Claude CLI, konfigurace)")
    p_doctor.add_argument(
        "--live", action="store_true",
        help="Navíc spustí skutečný (placený) test volání Claude Code CLI",
    )
    p_doctor.add_argument("--live-project", help="Adresář, ve kterém se má --live test spustit")
    p_doctor.set_defaults(func=cmd_doctor)

    p_run = sub.add_parser("run", help="Spustí jeden úkol a čeká na výsledek")
    p_run.add_argument("prompt", help="Zadání úkolu pro agenta")
    p_run.add_argument("--project", required=True, help="Jméno projektu z config.yaml, nebo cesta na disku")
    p_run.add_argument("--agent", help="Který agent se má použít (výchozí: default_agent z config.yaml)")
    p_run.add_argument("--test-command", help="Přepíše testovací příkaz pro tento běh")
    p_run_commit = p_run.add_mutually_exclusive_group()
    p_run_commit.add_argument(
        "--commit", action="store_true",
        help="Explicitně povolí commit pro tento běh (po úspěšných testech), i kdyby "
        "auto_commit v config.yaml bylo vypnuté",
    )
    p_run_commit.add_argument("--no-commit", action="store_true", help="Nikdy nevytvářet commit, i kdyby auto_commit bylo zapnuté")
    p_run.set_defaults(func=cmd_run)

    p_auto = sub.add_parser(
        "autonomous",
        help="Autonomní vývojový režim: opakuje implementace -> testy -> vyhodnocení -> oprava, "
        "dokud není splněná Definition of Done nebo není dosažen max. počet iterací",
    )
    p_auto.add_argument("--project", required=True, help="Jméno projektu z config.yaml, nebo cesta na disku")
    p_auto.add_argument("--goal", help="Stručný popis cíle projektu (kontext pro agenta)")
    p_auto.add_argument(
        "--spec",
        help="Cesta k souboru s Definition of Done (jeden bod na řádek, volitelně jako "
        "checklist '- [ ] ...'). Pokud je zadán i --goal, --spec určuje Definition of Done "
        "a --goal jen kontext cíle.",
    )
    p_auto.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
        help=f"Bezpečný maximální počet iterací (výchozí {DEFAULT_MAX_ITERATIONS}, "
        f"strop {ABSOLUTE_MAX_ITERATIONS})",
    )
    p_auto.add_argument("--run-id", help="Externí ID běhu předané nadřazeným orchestrátorem")
    p_auto.add_argument("--agent", help="Který agent se má použít (výchozí: default_agent z config.yaml)")
    p_auto.add_argument(
        "--model",
        help="Přesný model předaný vybranému explicitnímu agentovi; bez volby se použije konfigurace agenta",
    )
    p_auto.add_argument("--test-command", help="Přepíše testovací příkaz pro tento běh")
    p_auto.add_argument(
        "--implementation-only",
        action="store_true",
        help="Po ověřené implementaci a testech skončit před auditem; audit proběhne v samostatném workflow ticku",
    )
    p_auto_commit = p_auto.add_mutually_exclusive_group()
    p_auto_commit.add_argument(
        "--commit", action="store_true",
        help="Explicitně povolí commit pro tento běh (po splnění Definition of Done a "
        "úspěšných testech), i kdyby auto_commit v config.yaml bylo vypnuté",
    )
    p_auto_commit.add_argument("--no-commit", action="store_true", help="Nikdy nevytvářet commit, i kdyby auto_commit bylo zapnuté")
    p_auto.set_defaults(func=cmd_autonomous)

    p_status = sub.add_parser("status", help="Zobrazí stav úkolu/úkolů")
    p_status.add_argument("task_id", nargs="?", help="ID konkrétního úkolu (bez ID zobrazí seznam)")
    p_status.add_argument("--filter", help="Filtrovat seznam podle stavu (pending/running/done/failed/...)")
    p_status.add_argument("--limit", type=int, default=20)
    p_status.set_defaults(func=cmd_status)

    p_api = sub.add_parser("api", help="Spustí lokální HTTP API na 127.0.0.1")
    p_api.set_defaults(func=cmd_api)

    p_inbox = sub.add_parser("import-inbox", help="Načte *.json úkoly z inbox/ a zařadí je do fronty")
    p_inbox.set_defaults(func=cmd_import_inbox)

    p_projects = sub.add_parser("projects", help="Vypíše projekty zaregistrované v config.yaml")
    p_projects.set_defaults(func=cmd_projects)

    p_plan = sub.add_parser("plan-inbox", help="AI read-only příprava lidského Inbox požadavku")
    p_plan.add_argument("--agent", required=True, choices=["provider-broker"])
    p_plan.add_argument("--model", help="Přesný model vybraného plánovacího providera")
    p_plan.set_defaults(func=cmd_plan_inbox)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

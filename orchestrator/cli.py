"""Command-line entry point. See README.md for usage examples in Czech."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from orchestrator.autonomous import ABSOLUTE_MAX_ITERATIONS, AutonomousStatus, DEFAULT_MAX_ITERATIONS
from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.registry import build_agent
from orchestrator.config import load_config
from orchestrator.doctor import run_doctor
from orchestrator.models import TaskStatus
from orchestrator.service import OrchestratorService


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
            provider_order=(
                [name.strip() for name in args.provider_order.split(",") if name.strip()]
                if args.provider_order
                else None
            ),
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


def cmd_plan_inbox(args: argparse.Namespace) -> int:
    """Run one read-only provider turn to structure a human Inbox request."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Inbox planner input must be a JSON object")
        config = load_config()
        # Planning receives an empty disposable workspace. The provider can
        # reason over the supplied card text but cannot mutate a project
        # checkout. Provider-specific read-only modes add a second guard.
        safe_config = replace(
            config,
            gemini=replace(config.gemini, approval_mode="plan"),
            antigravity=replace(config.antigravity, mode="plan"),
            codex=replace(config.codex, sandbox_mode="read-only"),
        )
        if args.model:
            config_attr = {
                "gemini": "gemini",
                "antigravity": "antigravity",
                "claude-code": "claude_code",
                "codex": "codex",
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
            "souborů, nepoužívej git a nic neimplementuj. Z lidského zadání níže "
            "vytvoř pracovní karty pro Připraveno, přičemž každá karta musí "
            "představovat jeden koherentní implementační výsledek. Implementaci, "
            "konfiguraci, integraci, potřebné testy a dokumentaci nerozděluj jen "
            "podle souboru, vrstvy nebo workflow fáze. Standardní nezávislý audit "
            "ai-orchestratoru, testování, live evidence a verdikt "
            "accepted/rejected patří do auditní fáze téže karty, nevytvářej pro "
            "ně samostatnou kartu a nikdy nepoužívej work_type audit. "
            "Rozdělení použij jen pro odlišný samostatný výsledek, jinou "
            "projektovou identitu nebo skutečný technický předpoklad; pokud "
            "rozdělíš, vysvětli to v split_reason a uveď přímé závislosti v "
            "depends_on. Pokud rozdělení není nutné, vrať jednu kartu a "
            "split_reason vysvětli, proč jde o jeden koherentní výsledek. "
            "Všechny karty z tohoto jediného vstupu tvoří jeden Inbox batch a "
            "jeden projekt; nikdy do něj nemíchej jiný projekt nebo jinou Inbox "
            "kartu. Rozdělení smí popsat pouze samostatné pracovní kroky stejného "
            "projektu a zachovej jejich návaznosti. Rozděl velký požadavek na "
            "malé samostatné úkoly. Každý úkol musí mít "
            "unikátní číselnou prioritu v rozsahu 0 až 5.999999; vyšší číslo je "
            "vyšší priorita, ale nesmí překročit naléhavost celého zdrojového "
            "Inbox zadání bez konkrétního důvodu. Ke každé prioritě povinně "
            "doplň priority_reason s konkrétním důvodem vycházejícím pouze ze "
            "vstupu. Opravy PM/orchestrátoru a potvrzené regrese mají "
            "přednost před novými funkcemi. Zachovej výhradně informace ze vstupu, "
            "nevymýšlej projektovou identitu ani důkazy. U každého podúkolu "
            "uveď depends_on jako zero-based indexy přímých předpokladů. "
            "Závislosti musí tvořit acyklický graf; pokud jsou podúkoly "
            "nezávislé, vrať prázdné pole. Vrať pouze JSON ve tvaru "
            "work_type musí být jedna z hodnot implementation, research, "
            "configuration, integration nebo tests; tests použij samostatně "
            "jen pokud vstup výslovně požaduje samostatný testovací výsledek "
            "nebo jde o skutečný předpoklad. split_reason nesmí být prázdný. "
            "priority_reason u každého úkolu musí obsahovat konkrétní důvod "
            "pro jeho prioritu. "
            "{\"tasks\":[{\"scope\":\"...\",\"task\":\"...\","
            "\"next_step\":\"...\",\"priority\":3.01,"
            "\"priority_reason\":\"...\",\"work_type\":\"implementation\","
            "\"split_reason\":\"...\",\"depends_on\":[]}]}\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        with tempfile.TemporaryDirectory(prefix="ai-orchestrator-inbox-plan-") as workspace:
            result = agent.run(
                AgentRunRequest(
                    project_path=Path(workspace),
                    prompt=prompt,
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
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "integer", "minimum": 0, "maximum": 31},
                                        },
                                    },
                                    "required": [
                                        "scope", "task", "next_step", "priority",
                                        "priority_reason", "work_type", "split_reason",
                                        "depends_on",
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
        output = {
            "success": result.success,
            "provider": args.agent,
            # The PM deliberately does not pass --model for provider-owned
            # selection.  Report the model the provider actually returned;
            # only fall back to configured argv state when the provider did
            # not expose a receipt model (e.g. a mocked/legacy CLI).
            "model": result.model or getattr(
                getattr(safe_config, args.agent.replace("-", "_"), None),
                "model",
                None,
            ),
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
        "--provider-order",
        help="Volitelné pořadí providerů pouze pro tento failover běh; výchozí AO pořadí se nemění",
    )
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
    p_plan.add_argument("--agent", required=True, choices=["gemini", "antigravity", "claude-code", "codex"])
    p_plan.add_argument("--model", help="Přesný model vybraného plánovacího providera")
    p_plan.set_defaults(func=cmd_plan_inbox)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

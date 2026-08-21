"""Command-line entry point. See README.md for usage examples in Czech."""

from __future__ import annotations

import argparse
import sys

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
            auto_commit=(False if args.no_commit else None),
            source="cli",
        )
    except ValueError as e:
        print(f"Chyba: {e}")
        return 1

    print(f"Task {task.id} spuštěn na projektu '{task.project}' ({task.project_path})...")
    result_task = service.run_sync(task)
    return _print_task(result_task)


def _print_task(task) -> int:
    print(f"\n== Task {task.id} - {task.status.value} ==")
    if task.result:
        print("\n--- Výsledek agenta ---")
        print(task.result)
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
    p_run.add_argument("--no-commit", action="store_true", help="Nikdy nevytvářet commit, i kdyby auto_commit bylo zapnuté")
    p_run.set_defaults(func=cmd_run)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

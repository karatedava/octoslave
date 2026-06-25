"""Helper script: run the autonomous Lab from the CLI without an interactive REPL.

(The old fixed role pipeline has been retired and fully replaced by the dynamic
Lab — see octoslave/lab/.)
"""
from pathlib import Path

from octoslave.config import load_config
from octoslave.agent import make_client
from octoslave.lab.runner import run_lab


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("task_file", help="Path to a task file (the task is its contents)")
    p.add_argument("--working-dir", required=True)
    p.add_argument("--rounds", type=int, default=4, help="Max implementation/review rounds")
    p.add_argument("--model", default=None, help="Model for all roles (default: config / kimi-k2.6)")
    p.add_argument("--manual", action="store_true",
                   help="Step mode: pause at gates for human approval (default: autonomous)")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    task = Path(args.task_file).read_text().strip()
    cfg = load_config()
    client = make_client(cfg["api_key"], cfg["base_url"])

    run_lab(
        task=task,
        working_dir=args.working_dir,
        client=client,
        model=args.model or cfg.get("default_model"),
        autonomous=not args.manual,
        max_rounds=args.rounds,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()

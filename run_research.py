"""Helper script: run the long-research pipeline from the CLI without an interactive REPL."""
import sys
from pathlib import Path

from octoslave.config import load_config
from octoslave.agent import make_client
from octoslave.research import run_long_research, PIPELINE

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("task_file", help="Path to task.md (topic comes from file contents)")
    p.add_argument("--working-dir", required=True)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--model", default=None, help="Override all role models")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    topic = Path(args.task_file).read_text().strip()
    cfg = load_config()
    client = make_client(cfg["api_key"], cfg["base_url"])

    model_overrides = {role: args.model for role in PIPELINE} if args.model else None

    run_long_research(
        topic=topic,
        working_dir=args.working_dir,
        client=client,
        max_rounds=args.rounds,
        model_overrides=model_overrides,
        resume=args.resume,
    )

if __name__ == "__main__":
    main()

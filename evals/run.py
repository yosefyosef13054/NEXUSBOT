"""CLI: `python -m evals.run --dataset evals/datasets/chat_smoke.jsonl [--model ID] [--tags chat,tools]
[--fail-under 0.9] [--out report.json] [--email evals@nexusbot.local]`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.db.session import db_session
from evals.runner import ensure_eval_user, format_report, run_dataset


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a NexusBot eval dataset")
    p.add_argument("--dataset", required=True, help="JSONL file of cases")
    p.add_argument("--model", default=None, help="model id to evaluate (default: server default)")
    p.add_argument("--tags", default=None, help="comma-separated tag filter")
    p.add_argument("--email", default="evals@nexusbot.local", help="account that owns the eval sessions")
    p.add_argument("--out", default=None, help="write the JSON report here")
    p.add_argument("--fail-under", type=float, default=None, help="exit 1 when pass rate is below this")
    return p.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    async with db_session() as db:
        user = await ensure_eval_user(db, args.email)
        report = await run_dataset(db, path=args.dataset, user=user, model=args.model, tags=tags)
    print(format_report(report))
    if args.out:
        report.write(args.out)
        print(f"report written to {args.out}")
    if args.fail_under is not None and report.pass_rate < args.fail_under:
        print(
            f"pass rate {report.pass_rate:.2%} is below --fail-under {args.fail_under:.0%}", file=sys.stderr
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse(argv)))


if __name__ == "__main__":
    sys.exit(main())

"""The eval harness runs datasets through the real pipeline and grades replies."""

from __future__ import annotations

import json

import pytest

from app.ai.fake import script_responses, tool_call_message
from evals.runner import EvalCase, ensure_eval_user, format_report, load_dataset, run_dataset
from tests.conftest import requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]


async def test_eval_runner_grades_cases_and_reports(db, tmp_path):
    user = await ensure_eval_user(db, "evals-test@example.com")
    cases = [
        EvalCase(
            id="echo",
            turns=["Repeat exactly: nexusbot rocks"],
            expect={"contains": ["nexusbot rocks"], "max_length": 200},
        ),
        EvalCase(
            id="fails", turns=["hello"], expect={"contains": ["this will not appear"], "regex": r"^\(fake\)"}
        ),
        EvalCase(
            id="tool",
            turns=["6*7"],
            use_tools=True,
            tool_names=["calculator"],
            expect={"tool_called": "calculator", "contains": ["42"]},
        ),
        EvalCase(id="judge", turns=["be nice"], expect={"judge": {"rubric": "Reply is polite"}}),
        EvalCase(id="broken-model", turns=["x"], model="no-such-model", expect={"contains": ["x"]}),
        EvalCase(
            id="multi", turns=["first", "second"], expect={"contains": ["second"], "not_contains": ["first"]}
        ),
    ]
    # Scripted replies are consumed by *any* model call, so cases that need them run alone.
    report = await run_dataset(db, path="inline", user=user, cases=cases[:2])
    assert [r.passed for r in report.results] == [True, False]
    failed = {c.name: c for c in report.results[1].checks}
    assert failed["contains"].passed is False and failed["regex"].passed is True

    script_responses(tool_call_message("calculator", {"expression": "6*7"}), "The answer is 42")
    report = await run_dataset(db, path="inline", user=user, cases=cases[2:3])
    assert report.results[0].passed is True
    assert report.results[0].usage["prompt_tokens"] > 0 and report.results[0].latency_ms >= 0

    script_responses("Of course, happy to help!", '{"pass": true, "reason": "polite"}')
    report2 = await run_dataset(db, path="inline", user=user, cases=cases[3:])
    by_id = {r.id: r for r in report2.results}
    assert by_id["judge"].passed is True and by_id["judge"].checks[0].detail == "polite"
    assert by_id["broken-model"].passed is False and by_id["broken-model"].error
    assert by_id["multi"].passed is True

    summary = report2.summary()
    assert summary["total"] == 3 and summary["passed"] == 2 and summary["failed"] == 1
    assert 0 < summary["pass_rate"] < 1
    text = format_report(report2)
    assert "[PASS] judge" in text and "[FAIL] broken-model" in text and "2/3 passed" in text

    out = tmp_path / "report.json"
    report2.write(out)
    data = json.loads(out.read_text())
    assert data["summary"]["passed"] == 2 and len(data["results"]) == 3


async def test_shipped_smoke_dataset_loads_and_filters():
    cases = load_dataset("evals/datasets/chat_smoke.jsonl")
    assert {c.id for c in cases} >= {"greeting", "multi-turn", "calculator"}
    assert all(c.turns for c in cases)
    tools_only = load_dataset("evals/datasets/chat_smoke.jsonl", tags=["tools"])
    assert [c.id for c in tools_only] == ["calculator"] and tools_only[0].use_tools


def test_eval_cli_runs_smoke_dataset_with_fake_model(tmp_path):
    """The CLI entry point (own event loop) against the shipped dataset with the echo model."""
    import os
    import subprocess
    import sys

    out = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.run",
            "--dataset",
            "evals/datasets/chat_smoke.jsonl",
            "--tags",
            "chat",
            "--email",
            "evals-cli@example.com",
            "--out",
            str(out),
            "--fail-under",
            "0.5",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "LOG_LEVEL": "ERROR"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "passed" in proc.stdout and "report written" in proc.stdout
    data = json.loads(out.read_text())
    assert data["summary"]["total"] == 4 and data["summary"]["passed"] >= 2
    assert {r["id"] for r in data["results"]} == {"greeting", "echo-style", "no-leak", "multi-turn"}

"""Run eval datasets through the production generation path and grade the replies."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import User
from app.services import chat_service, session_service
from app.services.chat_service import TurnScope
from app.services.org_service import ensure_personal_org
from evals import graders


@dataclass
class EvalCase:
    id: str
    turns: list[str]
    expect: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    use_rag: bool = False
    use_tools: bool = False
    tool_names: list[str] | None = None
    collection_ids: list[str] | None = None
    system_prompt: str | None = None
    model: str | None = None
    temperature: float | None = 0.0
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvalCase:
        turns = raw.get("turns") or ([raw["input"]] if raw.get("input") else [])
        if not turns:
            raise ValueError(f"case {raw.get('id')!r} has neither 'input' nor 'turns'")
        return cls(
            id=str(raw.get("id") or f"case-{abs(hash(turns[0])) % 10_000}"),
            turns=[str(t) for t in turns],
            expect=dict(raw.get("expect") or {}),
            tags=[str(t) for t in raw.get("tags") or []],
            use_rag=bool(raw.get("use_rag", False)),
            use_tools=bool(raw.get("use_tools", False)),
            tool_names=raw.get("tool_names"),
            collection_ids=raw.get("collection_ids"),
            system_prompt=raw.get("system_prompt"),
            model=raw.get("model"),
            temperature=raw.get("temperature", 0.0),
            max_tokens=raw.get("max_tokens"),
        )


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    passed: bool
    checks: list[CheckResult]
    reply: str
    status: str
    model: str | None
    latency_ms: int
    usage: dict[str, int]
    cost_usd: float
    error: str | None = None


@dataclass
class EvalReport:
    dataset: str
    model: str | None
    results: list[CaseResult]
    started_at: float
    finished_at: float

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        latencies = [r.latency_ms for r in self.results if r.error is None]
        return {
            "dataset": self.dataset,
            "model": self.model,
            "total": self.total,
            "passed": self.passed,
            "failed": self.total - self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
            "total_tokens": sum(r.usage.get("total_tokens", 0) for r in self.results),
            "total_cost_usd": round(sum(r.cost_usd for r in self.results), 6),
            "duration_s": round(self.finished_at - self.started_at, 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "results": [asdict(r) for r in self.results]}

    def write(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, default=str))


def load_dataset(path: str | Path, *, tags: list[str] | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        case = EvalCase.from_dict(json.loads(line))
        if tags and not set(tags) & set(case.tags):
            continue
        cases.append(case)
    return cases


async def ensure_eval_user(db: AsyncSession, email: str) -> User:
    """The eval account that owns the sessions the harness creates."""
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, hashed_password=hash_password("evals-not-a-login"), full_name="Eval runner")
        db.add(user)
        await db.flush()
    await ensure_personal_org(db, user)
    await db.commit()
    return user


async def grade(case: EvalCase, *, reply: str, turn: dict[str, Any]) -> list[CheckResult]:
    exp = case.expect
    checks: list[CheckResult] = []
    meta = turn["assistant_message"].meta or {}

    def add(name: str, result: tuple[bool, str]) -> None:
        checks.append(CheckResult(name=name, passed=result[0], detail=result[1]))

    if "contains" in exp:
        add("contains", graders.check_contains(reply, list(exp["contains"])))
    if "not_contains" in exp:
        add("not_contains", graders.check_not_contains(reply, list(exp["not_contains"])))
    if "regex" in exp:
        add("regex", graders.check_regex(reply, str(exp["regex"])))
    if "equals" in exp:
        add("equals", graders.check_equals(reply, str(exp["equals"])))
    if "min_length" in exp:
        add("min_length", graders.check_min_length(reply, int(exp["min_length"])))
    if "max_length" in exp:
        add("max_length", graders.check_max_length(reply, int(exp["max_length"])))
    if "tool_called" in exp:
        add("tool_called", graders.check_tool_called(turn["tool_calls"], str(exp["tool_called"])))
    if "citations_min" in exp:
        add("citations_min", graders.check_citations_min(turn["citations"], int(exp["citations_min"])))
    if "status" in exp:
        add("status", graders.check_status(str(meta.get("status", "ok")), str(exp["status"])))
    if "judge" in exp:
        judge = exp["judge"] if isinstance(exp["judge"], dict) else {"rubric": str(exp["judge"])}
        add(
            "judge",
            await graders.check_judge(
                user_input=case.turns[-1],
                reply=reply,
                rubric=str(judge.get("rubric", "")),
                model=judge.get("model"),
            ),
        )
    if not checks:
        checks.append(
            CheckResult(name="ran", passed=bool(reply.strip()), detail="no checks declared; reply non-empty")
        )
    return checks


async def run_case(db: AsyncSession, *, user: User, case: EvalCase, model: str | None) -> CaseResult:
    scope = TurnScope(user_id=user.id, user_default_model=user.default_model, memory_enabled=False)
    started = time.perf_counter()
    turn: dict[str, Any] | None = None
    try:
        session_obj = await session_service.create_session(
            db,
            user_id=user.id,
            org_id=None,
            title=f"eval:{case.id}",
            system_prompt=case.system_prompt,
            model=case.model or model,
            meta={"eval": True},
        )
        await db.commit()
        for text in case.turns:
            turn = await chat_service.send_message(
                db,
                user_id=user.id,
                session_id=session_obj.id,
                content=text,
                use_rag=case.use_rag,
                use_tools=case.use_tools,
                tool_names=case.tool_names,
                model=case.model or model,
                collection_ids=[UUID(c) for c in case.collection_ids] if case.collection_ids else None,
                temperature=case.temperature,
                max_tokens=case.max_tokens,
                scope=scope,
                explicit={"use_rag", "use_tools"},
            )
    except Exception as e:  # noqa: BLE001 — a failing case must not abort the run
        await db.rollback()
        return CaseResult(
            id=case.id,
            tags=case.tags,
            passed=False,
            checks=[
                CheckResult(name="generation", passed=False, detail=f"{type(e).__name__}: {str(e)[:200]}")
            ],
            reply="",
            status="error",
            model=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage={},
            cost_usd=0.0,
            error=str(e)[:300],
        )
    assert turn is not None
    latency_ms = int((time.perf_counter() - started) * 1000)
    msg = turn["assistant_message"]
    reply = msg.content or ""
    checks = await grade(case, reply=reply, turn=turn)
    meta = msg.meta or {}
    usage = dict(turn.get("usage") or {})
    from app.utils.tokens import estimate_cost

    cost = estimate_cost(
        meta.get("model") or "",
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
    )
    return CaseResult(
        id=case.id,
        tags=case.tags,
        passed=all(c.passed for c in checks),
        checks=checks,
        reply=reply,
        status=str(meta.get("status", "ok")),
        model=meta.get("model"),
        latency_ms=latency_ms,
        usage=usage,
        cost_usd=cost,
    )


async def run_dataset(
    db: AsyncSession,
    *,
    path: str | Path,
    user: User,
    model: str | None = None,
    tags: list[str] | None = None,
    cases: list[EvalCase] | None = None,
) -> EvalReport:
    cases = cases if cases is not None else load_dataset(path, tags=tags)
    started = time.time()
    results = [await run_case(db, user=user, case=case, model=model) for case in cases]
    return EvalReport(
        dataset=str(path), model=model, results=results, started_at=started, finished_at=time.time()
    )


def format_report(report: EvalReport) -> str:
    lines = []
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        failed = [f"{c.name}: {c.detail}" for c in r.checks if not c.passed]
        extra = f"  ← {'; '.join(failed)}" if failed else ""
        lines.append(
            f"[{mark}] {r.id:<28} {r.latency_ms:>6} ms  {r.usage.get('total_tokens', 0):>6} tok{extra}"
        )
    s = report.summary()
    lines.append("")
    lines.append(
        f"{s['passed']}/{s['total']} passed ({s['pass_rate']:.0%}), avg {s['avg_latency_ms']} ms, "
        f"{s['total_tokens']} tokens, ${s['total_cost_usd']:.4f}"
    )
    return "\n".join(lines)

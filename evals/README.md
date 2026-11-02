# Evals

Regression tests for *quality*, not just correctness: each dataset is a list of chat cases
run through the real generation pipeline (`chat_service.send_message`: prompts, routing,
RAG, tools, memory) and graded with cheap deterministic checks or an LLM judge.

```bash
# fake model (no keys needed): checks the harness and prompt plumbing
AI_FAKE_MODE=true python -m evals.run --dataset evals/datasets/chat_smoke.jsonl

# a real model, gate CI on 90 % pass rate, keep the report
python -m evals.run --dataset evals/datasets/chat_smoke.jsonl --model gpt-4o-mini \
    --fail-under 0.9 --out reports/chat_smoke.json
```

A case (one JSON object per line):

```json
{"id": "greeting", "tags": ["chat"], "input": "Say hello in one word.",
 "use_rag": false, "use_tools": false, "tool_names": null,
 "expect": {"contains": ["hello"], "max_length": 80}}
```

`turns` (a list of user messages) replaces `input` for multi-turn cases; the last reply is
graded. Supported `expect` checks: `contains`, `not_contains` (lists, case-insensitive),
`regex`, `equals`, `min_length`, `max_length`, `tool_called` (tool name),
`citations_min` (count), `status` (`ok` / `awaiting_approval`), and
`judge` (`{"rubric": "...", "model": "<optional judge model>"}`) which asks a model for a
`{"pass": bool, "reason": str}` verdict.

The report lists every case with its checks, latency, tokens and estimated cost, plus a
summary (`pass_rate`, totals). `tests/test_evals.py` runs the harness against the fake
model so the plumbing itself is covered by CI.

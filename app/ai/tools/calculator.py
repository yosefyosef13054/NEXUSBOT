from __future__ import annotations

import ast
import operator as op
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.Pow: op.pow, ast.Mod: op.mod, ast.FloorDiv: op.floordiv,
    ast.USub: op.neg, ast.UAdd: op.pos,
}


def _safe_eval(expr: str) -> float:
    """AST-based evaluator: arithmetic only. Avoids `eval` injection."""
    tree = ast.parse(expr, mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"Unsupported expression: {ast.dump(node)}")

    return float(_eval(tree))


class CalculatorInput(BaseModel):
    expression: str = Field(description="A pure arithmetic expression, e.g. '2*(3+4)/5'.")


class CalculatorTool(BaseTool):
    name: str = "calculator"
    description: str = (
        "Evaluates a basic arithmetic expression (+ - * / ** % //). "
        "Use this whenever exact math is required."
    )
    args_schema: Type[BaseModel] = CalculatorInput

    def _run(self, expression: str) -> str:  # type: ignore[override]
        return str(_safe_eval(expression))

    async def _arun(self, expression: str) -> str:  # type: ignore[override]
        return self._run(expression)

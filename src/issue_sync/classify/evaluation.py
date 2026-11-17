"""Offline evaluation of the classifier against labelled cases (`issue-sync eval-classifier`).

Runs every case through a real `Classifier` and reports per-field accuracy, so prompt
changes and model switches can be compared with numbers instead of impressions.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict

from issue_sync.classify.base import Classification, Classifier, ClassifierError, ClassifierInput

FIELDS = ("type", "urgency", "assignee_role")


def _as_list(value: object) -> object:
    return [value] if isinstance(value, str) else value


Accepted = Annotated[list[str], BeforeValidator(_as_list)]


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Accepted
    urgency: Accepted
    assignee_role: Accepted


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str | None = None
    description: str | None = None
    expected: Expected


@dataclass
class CaseResult:
    case: Case
    seconds: float
    classification: Classification | None = None
    error: str | None = None

    def correct(self, name: str) -> bool:
        if self.classification is None:
            return False
        return getattr(self.classification.judgement, name) in getattr(self.case.expected, name)


@dataclass
class Report:
    results: list[CaseResult] = field(default_factory=list)

    def accuracy(self, name: str) -> float:
        return sum(r.correct(name) for r in self.results) / len(self.results)

    @property
    def all_correct(self) -> float:
        return sum(all(r.correct(f) for f in FIELDS) for r in self.results) / len(self.results)

    @property
    def errors(self) -> int:
        return sum(r.error is not None for r in self.results)

    def render(self) -> str:
        lines = []
        for r in self.results:
            if r.classification is None:
                lines.append(f"ERROR {r.case.id}: {r.error}")
                continue
            marks = " ".join(self._mark(r, name) for name in FIELDS)
            ok = "ok   " if all(r.correct(f) for f in FIELDS) else "MISS "
            lines.append(f"{ok} {r.case.id} [{r.seconds:.1f}s]: {marks}")
        latencies = sorted(r.seconds for r in self.results)
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        models = ", ".join(
            sorted({r.classification.model for r in self.results if r.classification})
        )
        lines += [
            "",
            f"cases: {len(self.results)}   errors: {self.errors}   model: {models or '-'}",
            *(f"{name:<14} accuracy: {self.accuracy(name):.0%}" for name in FIELDS),
            f"{'all three':<14} accuracy: {self.all_correct:.0%}",
            f"latency: avg {sum(latencies) / len(latencies):.1f}s, p95 {p95:.1f}s",
        ]
        return "\n".join(lines)

    @staticmethod
    def _mark(result: CaseResult, name: str) -> str:
        assert result.classification is not None
        text = f"{name}={getattr(result.classification.judgement, name)}"
        if not result.correct(name):
            text += f" (expected {'|'.join(getattr(result.case.expected, name))})"
        return text


def load_cases(path: Path) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: expected a non-empty list of cases")
    cases = [Case.model_validate(item) for item in data]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: case ids must be unique")
    return cases


async def evaluate(classifier: Classifier, cases: Sequence[Case], concurrency: int = 4) -> Report:
    semaphore = asyncio.Semaphore(concurrency)

    async def run(case: Case) -> CaseResult:
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await classifier.classify(
                    ClassifierInput(title=case.title, description=case.description)
                )
            except ClassifierError as exc:
                return CaseResult(case, time.perf_counter() - started, error=str(exc))
            return CaseResult(case, time.perf_counter() - started, classification=result)

    return Report(list(await asyncio.gather(*(run(c) for c in cases))))

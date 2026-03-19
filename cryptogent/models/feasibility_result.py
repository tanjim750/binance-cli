from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeasibilityResult:
    category: str
    warnings: list[str]
    rejection_reason: str | None = None


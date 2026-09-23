"""Interpretation notes: short, plain-language findings attached to each pipeline stage."""

from __future__ import annotations

from dataclasses import asdict, dataclass

INFO = "info"
GOOD = "good"
WARN = "warn"


@dataclass
class Note:
    stage: str
    level: str  # info | good | warn
    text: str

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        tag = {INFO: "•", GOOD: "✓", WARN: "!"}.get(self.level, "•")
        return f"[{self.stage}] {tag} {self.text}"

"""What one scripted agent did: each step, its time, and the text it put into the agent's context."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

BYTES_PER_TOKEN = 3.5
TOKEN_METHOD = "ceil(UTF-8 bytes / 3.5), engmem's own estimate (telemetry.estimate_tokens)"

TOOL = "tool"
TEMPLATE = "template"
PASTE = "paste"


def tokens(byte_count: int) -> int:
    """The Layer 1 token approximation; see `TOKEN_METHOD`."""
    return math.ceil(byte_count / BYTES_PER_TOKEN) if byte_count > 0 else 0


@dataclass
class Step:
    """One action: a tool call, a template the host injected, or output a user pasted back."""

    phase: str
    kind: str
    action: str
    seconds: float
    text: str
    ok: bool = True
    cite: dict[str, str] = field(default_factory=dict)
    detail: dict[str, int] = field(default_factory=dict)
    source: str = ""

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "action": self.action,
            "seconds": round(self.seconds, 4),
            "bytes": self.bytes,
            "tokens": tokens(self.bytes),
            "ok": self.ok,
            **({"detail": self.detail} if self.detail else {}),
            **({"source": self.source} if self.source else {}),
        }


@dataclass
class Transcript:
    """Steps in order; the agent's context is the concatenation of their text."""

    steps: list[Step] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    def context(self) -> str:
        return "\n".join(s.text for s in self.steps if s.kind != TEMPLATE)

    def calls(self, phase: str | None = None) -> int:
        return sum(1 for s in self.steps if s.kind != TEMPLATE and (phase is None or s.phase == phase))

    def bytes_of(self, kind: str, phase: str | None = None) -> int:
        return sum(s.bytes for s in self.steps if s.kind == kind and (phase is None or s.phase == phase))

    def seconds(self, phase: str | None = None) -> float:
        return sum(s.seconds for s in self.steps if phase is None or s.phase == phase)

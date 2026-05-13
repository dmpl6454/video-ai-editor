"""Append-only ops log: human-readable history of every EDL mutation."""
from __future__ import annotations
import time
from typing import Any
from pydantic import BaseModel, Field


class Op(BaseModel):
    seq: int
    ts: float
    tool: str
    args: dict[str, Any]
    summary: str
    edl_hash_before: str
    edl_hash_after: str
    by: str = "user"  # "user" | "claude"


class OpsLog(BaseModel):
    ops: list[Op] = Field(default_factory=list)

    def append(self, tool: str, args: dict[str, Any], summary: str,
               edl_hash_before: str, edl_hash_after: str, by: str = "user") -> Op:
        op = Op(
            seq=len(self.ops),
            ts=time.time(),
            tool=tool,
            args=args,
            summary=summary,
            edl_hash_before=edl_hash_before,
            edl_hash_after=edl_hash_after,
            by=by,
        )
        self.ops.append(op)
        return op

    def last(self) -> Op | None:
        return self.ops[-1] if self.ops else None

    def pop(self) -> Op | None:
        return self.ops.pop() if self.ops else None

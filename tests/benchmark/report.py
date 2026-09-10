"""The scoring report (spec §6.3): one record per case, totals per brain, the
two headline numbers (fast tier, slow tier), the machine line and the
first-use download list — written as JSON and Markdown.

The Markdown is what docs/BENCHMARK.md's "honest claim" (§6.5) is copied
from, verbatim: a number that is not in a report file is not a claim this
project makes. Nothing here interprets a result; `passed` is the conjunction
of the case's own assertions plus `verifier_drift` (the app's `verify` event
disagreeing with the independent measurement), and an unmeasurable assertion
(`pass: null`) is listed but never counted either way.

WHY the download list probes the machine rather than reading the spec's
table: the claim "small: cached; large-v3 3.1 GB; MADLAD 3 GB; Piper hi_IN
60 MB" is a fact about the build machine at report time. The sizes come from
`facts.FIRST_USE_BYTES` (one source), the cached/not-cached column from the
same `Path.exists` probes the product uses.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.prompt.facts import FIRST_USE_BYTES

from .measure import Assertion

REPORT_DIR = Path(os.environ.get("VAI_BENCH_REPORT") or
                  (_pu.user_cache_dir("Video AI Editor") / "bench" / "reports"))

#: The artefacts the README's first-use list names, in that order.
FIRST_USE_LISTED: tuple[str, ...] = ("whisper:small", "whisper:large-v3", "madlad", "piper:hi_IN-priyamvada-medium")


@dataclass
class CaseRecord:
    id: int
    name: str
    prompt: str
    tier: str
    parity: str
    brain: str | None
    content_brain: str | None
    passed: bool | None                   # None = skipped
    assertions: list[dict[str, Any]]
    verify_agreement: bool | None
    wall_s: float
    steps: list[str]
    skipped: list[str]
    downloads_needed: list[dict[str, Any]]
    skip_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    reply: str = ""

    @property
    def failed_assertions(self) -> list[dict[str, Any]]:
        return [a for a in self.assertions if a.get("pass") is False]

    @classmethod
    def skipped_case(cls, case: Any, reason: str) -> "CaseRecord":
        return cls(id=case.id, name=case.name, prompt=case.prompt, tier=case.tier, parity=case.parity,
                   brain=None, content_brain=None, passed=None, assertions=[], verify_agreement=None,
                   wall_s=0.0, steps=[], skipped=[], downloads_needed=[], skip_reason=reason)


def verify_agrees(verify_event: dict[str, Any] | None, case_passed: bool) -> tuple[bool | None, str]:
    """The app's headline verdict (`passed == total` over measurable
    headline checks) must match the independent one. None when the app
    measured nothing headline-worthy."""
    if not verify_event or not verify_event.get("total"):
        return None, "the app measured no headline check"
    app_ok = int(verify_event.get("passed", 0)) == int(verify_event.get("total", 0))
    if app_ok == case_passed:
        return True, ""
    failed = [c.get("check") for c in verify_event.get("checks", []) if c.get("pass") is False]
    return False, (f"app says {'pass' if app_ok else 'fail'} ({verify_event.get('passed')}/{verify_event.get('total')}"
                   f"{', failed: ' + ', '.join(map(str, failed)) if failed else ''}), benchmark says "
                   f"{'pass' if case_passed else 'fail'}")


def build_record(case: Any, run: Any, assertions: list[Assertion], *, extra: list[Assertion] = ()) -> CaseRecord:
    """Fold a case's run and its assertions into the record; `extra` are the
    harness-level assertions every case gets (via-prefix, done, op, wall
    clock, egress …). `verifier_drift` compares the app's verdict with the
    CASE's own measurements only: the app cannot know about a wall-clock cap
    or a socket guard, so a harness-only failure must not be double-counted
    as 'the verifier disagrees' (it was — a 14 s wall on case 1 read as a
    verifier defect)."""
    rows = [a.as_dict() for a in (*assertions, *extra)]
    case_rows = [a.as_dict() for a in assertions]
    measured = [a for a in case_rows if a["pass"] is not None]
    case_ok = all(a["pass"] for a in measured)
    agree, why = verify_agrees(run.verify, case_ok)
    rows.append({"name": "verifier_drift", "pass": agree, "measured": why or "agrees",
                 "expected": "the app's verify event agrees with the independent measurement", "detail": ""})
    passed = all(a["pass"] for a in rows if a["pass"] is not None) and agree is not False
    plan = run.plan or {}
    return CaseRecord(
        id=case.id, name=case.name, prompt=case.prompt, tier=case.tier, parity=case.parity,
        brain=run.brain, content_brain=plan.get("content_brain"), passed=passed, assertions=rows,
        verify_agreement=agree, wall_s=round(run.wall_s, 2),
        steps=[f"{s.get('tool')}:{s.get('status')}" for s in run.steps()],
        skipped=list(run.skipped_tools), downloads_needed=list(plan.get("downloads_needed") or []),
        errors=list(run.errors), reply=run.reply[:400])


# --------------------------------------------------------------------------
# machine + first-use facts
# --------------------------------------------------------------------------

def _sysctl(key: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5,
                             **_pu.SUBPROCESS_FLAGS).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def _ffmpeg_version() -> str:
    try:
        out = subprocess.run([_pu.FFMPEG, "-version"], capture_output=True, text=True, timeout=5,
                             **_pu.SUBPROCESS_FLAGS).stdout.splitlines()
        return out[0].split("Copyright", 1)[0].strip() if out else "ffmpeg ?"
    except (OSError, subprocess.SubprocessError):
        return "ffmpeg ?"


def machine_facts() -> dict[str, Any]:
    ram_bytes = None
    raw = _sysctl("hw.memsize")
    if raw and raw.isdigit():
        ram_bytes = int(raw)
    elif hasattr(os, "sysconf"):
        try:
            ram_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            ram_bytes = None
    os_name = f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else f"{platform.system()} {platform.release()}"
    return {"chip": _sysctl("machdep.cpu.brand_string") or platform.processor() or platform.machine(),
            "arch": platform.machine(), "ram_gb": round(ram_bytes / 2**30) if ram_bytes else None,
            "os": os_name, "python": platform.python_version(), "ffmpeg": _ffmpeg_version()}


def machine_line(facts: dict[str, Any] | None = None) -> str:
    f = facts or machine_facts()
    return f"{f['chip']} ({f['arch']}), {f['ram_gb']} GB, {f['os']}, Python {f['python']}, {f['ffmpeg']}"


def first_use_status() -> list[dict[str, Any]]:
    """`[{key, label, bytes, cached}]` for the listed artefacts, from the
    product's own probes."""
    from video_ai_editor.agent.dispatch import whisper_model_on_disk
    from video_ai_editor.ai import translate as _tr
    from video_ai_editor.ai import tts as _tts
    rows: list[dict[str, Any]] = []
    for key in FIRST_USE_LISTED:
        nbytes, label = FIRST_USE_BYTES[key]
        if key.startswith("whisper:"):
            cached = whisper_model_on_disk(key.split(":", 1)[1])
        elif key == "madlad":
            d = _tr._model_dir()
            cached = d.exists() and any(d.iterdir())
        else:
            cached = _tts.voice_paths(key.split(":", 1)[1])[0].exists()
        rows.append({"key": key, "label": label, "bytes": nbytes, "cached": bool(cached)})
    return rows


def first_use_line(rows: list[dict[str, Any]] | None = None) -> str:
    rows = rows or first_use_status()
    bits = []
    for r in rows:
        short = r["label"].split(" (")[0]
        size = r["label"].split("(")[-1].rstrip(")")
        bits.append(f"{short}: {'cached' if r['cached'] else size}")
    return "; ".join(bits)


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

@dataclass
class Report:
    brain: str | None
    records: list[CaseRecord] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    machine: dict[str, Any] = field(default_factory=machine_facts)
    first_use: list[dict[str, Any]] = field(default_factory=first_use_status)
    generated_at: float = field(default_factory=time.time)

    def add(self, record: CaseRecord) -> None:
        self.records = [r for r in self.records if r.id != record.id] + [record]
        self.records.sort(key=lambda r: r.id)

    def tier(self, name: str) -> list[CaseRecord]:
        return [r for r in self.records if r.tier == name]

    @staticmethod
    def score(rows: list[CaseRecord]) -> tuple[int, int, int]:
        """(passed, ran, skipped)."""
        ran = [r for r in rows if r.passed is not None]
        return sum(1 for r in ran if r.passed), len(ran), len(rows) - len(ran)

    def per_brain(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.records:
            if r.passed is None:
                continue
            row = out.setdefault(r.brain or "?", {"passed": 0, "ran": 0})
            row["ran"] += 1
            row["passed"] += int(r.passed)
        return out

    #: Ids of every case the suite defines; set by the harness so a partial run
    #: (`-k`, `-x`, a tier marker) is reported as PARTIAL with the missing
    #: cases listed as "not run", never as a smaller denominator.
    expected_ids: tuple[int, ...] = ()

    @property
    def not_run(self) -> list[int]:
        have = {r.id for r in self.records}
        return [i for i in self.expected_ids if i not in have]

    @property
    def partial(self) -> bool:
        return bool(self.not_run)

    def headline(self) -> dict[str, str]:
        fp, fr, fs = self.score(self.tier("fast"))
        sp, sr, ss = self.score(self.tier("slow"))
        out = {"fast": f"{fp}/{fr} (skipped {fs})", "slow": f"{sp}/{sr} (skipped {ss})"}
        if self.partial:
            n, total = len(self.records), len(self.expected_ids)
            out["partial"] = f"PARTIAL RUN ({n}/{total} cases; not run: {', '.join(map(str, self.not_run))})"
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"brain": self.brain, "generated_at": self.generated_at, "machine": self.machine,
                "machine_line": machine_line(self.machine), "first_use": self.first_use,
                "first_use_line": first_use_line(self.first_use), "headline": self.headline(),
                "partial": self.partial, "not_run": self.not_run,
                "per_brain": self.per_brain(), "timings": self.timings,
                "cases": [asdict(r) for r in self.records]}

    def to_markdown(self) -> str:
        h = self.headline()
        lines = [f"# CapCut-parity benchmark — brain `{self.brain or 'auto'}`", "",
                 f"Generated {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.generated_at))} on "
                 f"{machine_line(self.machine)}.", "",
                 f"**Fast tier: {h['fast']} · Slow tier: {h['slow']}**"
                 + (f" — **{h['partial']}**" if self.partial else ""), "",
                 "Per brain: " + (", ".join(f"`{b}` {v['passed']}/{v['ran']}" for b, v in self.per_brain().items()) or "—"), "",
                 f"First-use downloads on this machine: {first_use_line(self.first_use)}.", "",
                 "| # | Prompt | Parity | Brain | Result | Wall s | Failed / unmeasured |",
                 "|---|---|---|---|---|---|---|"]
        for r in self.records:
            if r.passed is None:
                lines.append(f"| {r.id} | {r.prompt} | {r.parity} | — | skipped: {r.skip_reason} | — | — |")
                continue
            failed = [a["name"] for a in r.assertions if a.get("pass") is False]
            unmeasured = [a["name"] for a in r.assertions if a.get("pass") is None]
            notes = ", ".join(failed) + (f" · unmeasured: {', '.join(unmeasured)}" if unmeasured else "")
            brain = r.brain or "?"
            if r.content_brain and r.content_brain != r.brain:
                brain += f" · text by {r.content_brain}"
            lines.append(f"| {r.id} | {r.prompt} | {r.parity} | {brain} | {'pass' if r.passed else 'FAIL'} | "
                         f"{r.wall_s:.1f} | {notes or '—'} |")
        if self.timings:
            lines += ["", "Fixture timings (s): " + ", ".join(f"{k}={v}" for k, v in sorted(self.timings.items()))]
        lines += ["", "Not covered: object removal, background removal, motion tracking, multicam, licensed music "
                  "(beds are procedural). Beat sync places pulses and word-boundary-safe splits on detected beats; "
                  "it does not re-arrange footage."]
        return "\n".join(lines) + "\n"

    def write(self, out_dir: Path | None = None) -> tuple[Path, Path]:
        """Always writes the stamped files; refreshes `latest.*` — the files
        docs/BENCHMARK.md quotes — only when every case was selected, so a
        `-k transitions` run can never leave 'Fast tier: 3/3' as the claim."""
        out_dir = Path(out_dir or REPORT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.generated_at))
        stem = f"{stamp}_{self.brain or 'auto'}" + ("_partial" if self.partial else "")
        js = out_dir / f"{stem}.json"
        md = out_dir / f"{stem}.md"
        js.write_text(json.dumps(self.as_dict(), indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        md.write_text(self.to_markdown(), encoding="utf-8")
        if not self.partial:
            (out_dir / "latest.md").write_text(self.to_markdown(), encoding="utf-8")
            (out_dir / "latest.json").write_text(js.read_text(encoding="utf-8"), encoding="utf-8")
        return js, md


__all__ = ["REPORT_DIR", "FIRST_USE_LISTED", "CaseRecord", "verify_agrees", "build_record",
           "machine_facts", "machine_line", "first_use_status", "first_use_line", "Report"]

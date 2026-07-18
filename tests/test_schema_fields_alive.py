"""Dead-schema-field tripwire.

Every field on a renderable EDL model must be referenced somewhere in a
CONSUMER layer — render/, show/, or frontend/src/ — not merely written by
dispatch handlers. A field that is accepted, persisted, and read by nothing is
a silent lie to the user: exactly the bug class behind TextClip.anim_in/
anim_out (documented, set by a shipped template, rendered by nothing),
BrandKit.end_card/palette/font, TextClip.speaker, and TextStyle.color/font —
all of which shipped dead and were only caught by a manual audit (2026-07-17).

This is a word-boundary grep, so it is a TRIPWIRE, not a proof: a field whose
name collides with an unrelated identifier in a consumer layer (e.g. a
ROLE_STYLES dict key) can pass while still being per-clip dead. It exists to
force an explicit decision — wire the field, delete it, or allowlist it here
WITH a reason — whenever a new schema field lands.
"""
from __future__ import annotations
import re
from pathlib import Path

import video_ai_editor.edl.schema as schema

# Models whose fields the render/preview layers are expected to consume.
RENDERABLE_MODELS = [
    "Transform", "Effect", "Mask", "ChromaKey", "TextStyle", "TextClip",
    "Sticker", "Transition", "BrandKit", "AudioProps",
]

# (model, field) pairs that are knowingly not consumed yet. Every entry MUST
# say why. Additions to this list should be rare and deliberate.
ALLOWED_UNREAD: dict[tuple[str, str], str] = {
    ("Clip", "matte_src"): "bookkeeping for a future matte pipeline; not rendered yet",
    ("Clip", "track_to"): "motion_track writes it as provenance; keyframes carry the result",
}

# Field names too short/generic for a grep to mean anything.
MIN_NAME_LEN = 4


def _consumer_haystack() -> str:
    repo = Path(schema.__file__).resolve().parents[3]
    chunks: list[str] = []
    for rel in ("src/video_ai_editor/render", "src/video_ai_editor/show"):
        for p in (repo / rel).rglob("*.py"):
            chunks.append(p.read_text(encoding="utf-8"))
    fe = repo / "frontend" / "src"
    if fe.exists():
        for p in fe.rglob("*.ts*"):
            chunks.append(p.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_every_renderable_schema_field_has_a_consumer():
    hay = _consumer_haystack()
    assert len(hay) > 10_000, "consumer haystack looks wrong — path bug?"
    dead: list[str] = []
    for model_name in RENDERABLE_MODELS:
        model = getattr(schema, model_name)
        for fname in model.model_fields:
            if len(fname) < MIN_NAME_LEN:
                continue
            if (model_name, fname) in ALLOWED_UNREAD:
                continue
            if not re.search(rf"\b{re.escape(fname)}\b", hay):
                dead.append(f"{model_name}.{fname}")
    assert not dead, (
        "schema fields with NO reference in any consumer layer (render/, show/, "
        f"frontend/src/): {dead}. Wire the field into a renderer, delete it, or "
        "allowlist it in this test with a reason."
    )

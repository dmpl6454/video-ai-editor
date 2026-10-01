"""The text a turn puts in front of a plan's own reply, within the `Plan.reply` cap."""
from __future__ import annotations

#: `Plan.reply` is at most this long (schema); a turn's notes in front of a plan's own reply must fit under it.
REPLY_MAX = 400


def joined_reply(notes: list[str], reply: str | None) -> str:
    """The turn's notes ("Dropped the earlier question…") followed by the plan's own reply, within `Plan.reply`'s cap:
    the notes stay whole and the plan's reply gives way, at a word, with an ellipsis. Joined with no cap this raised a
    validation error out of the turn once a brain plan's long reply followed a note (closer: the Prompt bar ended on
    a traceback after an unanswered analysis question)."""
    head = " ".join(n for n in notes if n)
    if not head:
        return _cut_at_word(reply or "", REPLY_MAX)
    if len(head) >= REPLY_MAX:
        return _cut_at_word(head, REPLY_MAX)
    if not reply:
        return head
    room = REPLY_MAX - len(head) - 1
    return f"{head} {reply}" if len(reply) <= room else (f"{head} {_cut_at_word(reply, room)}" if room >= 24 else head)


def _cut_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:max(0, limit - 1)]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(" ,;:-–—") + "…"


__all__ = ["REPLY_MAX", "joined_reply"]

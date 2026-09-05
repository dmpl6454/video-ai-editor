/**
 * The op cursor, which had no test at all and was wrong.
 *
 * `lib/store.ts` is the one module in the app with a network protocol detail
 * baked into it that neither `api.test.ts` nor `connection.test.ts` could see:
 * `GET /api/sessions/{sid}/ops?since=N` is a SLICE (`store.ops.ops[N:]` in
 * main.py), and `seq` only equals the index because `edl/ops_log.py::append`
 * sets `seq = len(self.ops)`. Sending the last seq back re-returns that same op
 * on every poll for as long as the screen is open.
 *
 * The user-visible cost was specific: a fresh project has one op (`init`, seq
 * 0), so `edit.tsx` rendered "The project changed while you were looking at it
 * — Initial empty project" every six seconds on a project nobody had touched,
 * Dismiss cleared it for six seconds, and each poll also refetched the EDL and
 * the session for nothing.
 */

import { nextOpCursor } from "../../lib/store";
import type { Op } from "../../lib/types";

function op(seq: number, tool = "trim_clip"): Op {
  return { seq, tool, args: {}, summary: `${tool} ${seq}`, ts: 0 } as unknown as Op;
}

describe("nextOpCursor", () => {
  it("is one PAST the last op, because `since` is an index and not a seq", () => {
    expect(nextOpCursor([op(0)])).toBe(1);
    expect(nextOpCursor([op(0), op(1), op(2)])).toBe(3);
  });

  it("keeps the current cursor when the Mac returned nothing", () => {
    expect(nextOpCursor([], 7)).toBe(7);
    expect(nextOpCursor([])).toBe(0);
  });

  it("does not re-request the op it has just seen", () => {
    // The regression, stated as the round trip it actually is: open a fresh
    // project (one `init` op at seq 0), poll, and the Mac must have nothing to
    // say. With `since = 0` it answers `[init]` forever.
    const initial = [op(0, "init")];
    const cursor = nextOpCursor(initial);
    const serverOps = initial;
    const returned = serverOps.slice(cursor); // exactly what main.py:797 does
    expect(returned).toEqual([]);
  });

  it("advances past a run of ops the Mac made while we were away", () => {
    const log = [op(0, "init"), op(1), op(2), op(3)];
    let cursor = nextOpCursor([log[0] as Op]);
    const firstPoll = log.slice(cursor);
    expect(firstPoll.map((o) => o.seq)).toEqual([1, 2, 3]);
    cursor = nextOpCursor(firstPoll, cursor);
    expect(log.slice(cursor)).toEqual([]);
  });
});

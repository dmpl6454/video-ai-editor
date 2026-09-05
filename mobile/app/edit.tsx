/**
 * The editor: preview, timeline, and the small set of gestures a phone is
 * actually good at.
 *
 * FOUR THINGS THIS SCREEN IS CAREFUL ABOUT.
 *
 * 1. IT NEVER POINTS THE PLAYER AT AN UNRENDERED HASH. `preview.mp4` renders
 *    synchronously when the file is missing (`main.py:995`), which parks a
 *    request worker on the Mac for half a minute while AVFoundation gives up
 *    and shows black. `ensurePreview()` asks for the render as a job first.
 *
 * 2. THE MAC AND THE PHONE SHARE ONE PROJECT AND ONE UNDO STACK. There is no
 *    change channel, so `pollOps()` runs every six seconds while this screen
 *    is in front, and Undo always names the edit it is about to take back —
 *    on a shared stack, "Undo" with no subject is a guess.
 *
 * 3. A FAILED DISPATCH IS NEVER REPLAYED. `lib/store.ts` refetches instead.
 *    The Mac may have applied the edit before the connection dropped, and a
 *    replayed `ripple_delete` deletes twice.
 *
 * 4. IT SAYS WHAT IT CANNOT DO. Every action here is a request to the Mac. If
 *    the Mac is not there, the screen says so once, at the top, and disables
 *    the controls rather than letting them fail one at a time.
 */

import { router, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState, View } from "react-native";

import { ConnectionBar } from "../components/ConnectionBar";
import { PreviewPlayer, usePreviewPlayer } from "../components/PreviewPlayer";
import { TimelineStrip } from "../components/TimelineStrip";
import {
  Body,
  Button,
  Caption,
  Card,
  Heading,
  MacRequired,
  Note,
  Row,
  Screen,
  Stat,
  useAnnounce,
} from "../components/ui";
import { space } from "../constants/theme";
import { findClip, inSessionPosterSpec, timelineExtent, videoExtent } from "../lib/edl";
import { errorMessage, isCancellation } from "../lib/errors";
import { basename, humanDuration, shortHash, timecode } from "../lib/format";
import { ensurePreview, isPreviewStale, previewBackend, type PreviewSource } from "../lib/preview";
import { OPS_POLL_MS, selectMediaEnabled, useStore } from "../lib/store";
import {
  clampPixelsPerSecond,
  clipAt,
  DEFAULT_PIXELS_PER_SECOND,
  stripClipsOf,
  thumbPath,
  type StripClip,
} from "../lib/timeline";
import type { Op } from "../lib/types";

/** A time update that arrives within this window of a scrub is stale — it was
 *  already in flight when the seek was issued — and would drag the playhead
 *  back to where the user just left. */
const SCRUB_SETTLE_MS = 400;

export default function Edit() {
  const { sid } = useLocalSearchParams<{ sid?: string }>();

  const conn = useStore((s) => s.conn);
  const client = useStore((s) => s.client);
  const probe = useStore((s) => s.probe);
  const edl = useStore((s) => s.edl);
  const session = useStore((s) => s.session);
  const sessionId = useStore((s) => s.sessionId);
  const busy = useStore((s) => s.busy);
  const openSession = useStore((s) => s.openSession);
  const pollOps = useStore((s) => s.pollOps);
  const dispatch = useStore((s) => s.dispatch);
  const mediaEnabled = useStore(selectMediaEnabled);

  const connected = conn.status === "connected";
  const ready = connected && sessionId === sid && edl !== null;

  const [error, setError] = useState<string | null>(null);
  const [remoteEdit, setRemoteEdit] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null);
  const [pixelsPerSecond, setPixelsPerSecond] = useState(DEFAULT_PIXELS_PER_SECOND);

  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const [renderStatus, setRenderStatus] = useState<string | null>(null);
  const [renderProgress, setRenderProgress] = useState<number | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [mediaWarning, setMediaWarning] = useState<string | null>(null);
  const lastScrubAt = useRef(0);
  const probedSession = useRef<string | null>(null);
  /**
   * Op sequence numbers this phone caused.
   *
   * `pollOps()` asks for everything since the last seq it saw, and a dispatch
   * made HERE lands in that same log — so without this the phone would
   * announce its own edit back to itself as "the project changed while you
   * were looking at it" after every single tap.
   */
  const ownOpSeqs = useRef<Set<number>>(new Set());

  // -- open the project -----------------------------------------------------

  useEffect(() => {
    if (!sid || !connected) return;
    openSession(sid).catch((e: unknown) => setError(errorMessage(e)));
  }, [sid, connected, openSession]);

  // -- derived shape --------------------------------------------------------

  const clips = useMemo<StripClip[]>(() => stripClipsOf(edl), [edl]);
  const extent = useMemo(() => timelineExtent(edl), [edl]);
  const hasVideo = videoExtent(edl) > 0;
  const canvas = edl?.canvas ?? null;
  const fps = canvas?.fps ?? 30;
  const aspect = canvas && canvas.h > 0 ? canvas.w / canvas.h : 16 / 9;
  const edlHash = session?.summary.edl_hash ?? null;

  // -- preview --------------------------------------------------------------

  const headers = useMemo(() => client?.mediaHeaders() ?? {}, [client]);
  // Re-sign the preview URL when the player errors mid-stream. An mp4 is
  // fetched with successive Range requests against the ONE stored URL, and the
  // `?k=` token in it lives sixty seconds — so playing past the first minute,
  // or scrubbing back after a pause, used to 401 and stall with no explanation.
  const resignPreview = useCallback(
    (path: string) => {
      if (!client) return Promise.reject(new Error("Not connected."));
      return client.mediaUrl(path);
    },
    [client],
  );
  const controller = usePreviewPlayer(preview, headers, resignPreview);

  // Guarded by the hash rather than by a boolean: the effect re-runs whenever
  // the project changes, including changes made at the Mac, and re-rendering
  // the same hash twice would spend one of only two job workers for nothing.
  const renderingHash = useRef<string | null>(null);

  const render = useCallback(
    async (force = false) => {
      if (!client || !sid || !connected || !hasVideo) return;
      if (!force && renderingHash.current === edlHash) return;
      renderingHash.current = edlHash;
      const signal = { aborted: false };
      setPreviewError(null);
      try {
        const source = await ensurePreview(previewBackend(client), sid, {
          signal,
          onStatus: (line, progress) => {
            setRenderStatus(line);
            setRenderProgress(progress);
          },
        });
        setPreview(source);
        setRenderStatus(null);
      } catch (e) {
        setRenderStatus(null);
        if (isCancellation(e)) return;
        // A failed render is the preview's problem, not the project's: the
        // timeline is still correct and still editable, so this does not go
        // into the screen-level error banner.
        setPreviewError(errorMessage(e));
        renderingHash.current = null;
      }
    },
    [client, sid, connected, hasVideo, edlHash],
  );

  useEffect(() => {
    if (!ready) return;
    void render();
  }, [ready, edlHash, render]);

  // Not "stale" while a render for the new hash is already in flight — the
  // progress line below is telling that story, and offering "Re-render" beside
  // it would invite the user to queue a second job on a two-worker machine.
  const stale = isPreviewStale(preview, edlHash) && renderStatus === null;

  /**
   * One media probe per session.
   *
   * Nothing on the machine this app is built from can test whether iOS's own
   * video and image loaders keep our credentials across a range request — there
   * is no simulator here. `probeMedia` proves the half that lives on the Mac:
   * that a request signed only by the query token is accepted on a real media
   * path. If that passes and the player is still black, the fault is in the
   * player and the app can say so instead of blaming the render.
   *
   * It deliberately probes a THUMBNAIL rather than the preview: the probe reads
   * the whole response, and reading a whole mp4 to prove a point would be worse
   * than not proving it.
   */
  useEffect(() => {
    if (!client || !sid || !mediaEnabled || probedSession.current === sid) return;
    // The probe target MUST be a clip inside the session directory. `posterSpec`
    // returns the first video clip's raw `src`, which is routinely an external
    // path (`add_clip` and `find_broll` put `~/Movies/…` on the timeline) — and
    // `/thumb` answers 403 for those by design. Probing one turned a permanent
    // per-clip fact into an app-wide "Video and thumbnails may not load"
    // banner, rendered above a preview that was playing perfectly, for the
    // whole visit. No in-session clip means no probe: silence beats a warning
    // we cannot stand behind.
    const spec = inSessionPosterSpec(edl, sid);
    if (!spec) return;
    probedSession.current = sid;
    void client.probeMedia(thumbPath(sid, spec.src, spec.t)).then((result) => {
      // A `forbidden` here would mean the Mac answered and enforced its
      // boundary — which proves media auth WORKS. Only a network or auth
      // failure says the pictures are in trouble.
      const credible = !result.ok && result.kind !== "forbidden" && result.kind !== "not_found";
      setMediaWarning(credible ? result.detail : null);
    });
  }, [client, sid, mediaEnabled, edl]);

  // -- playhead -------------------------------------------------------------

  useEffect(() => {
    if (Date.now() - lastScrubAt.current < SCRUB_SETTLE_MS) return;
    setPlayhead(controller.currentTime);
  }, [controller.currentTime]);

  const onScrub = useCallback(
    (t: number) => {
      lastScrubAt.current = Date.now();
      setPlayhead(t);
      controller.seekTo(t);
    },
    [controller],
  );

  // -- the Mac's own edits --------------------------------------------------

  const watchOps = useCallback(async () => {
    if (!connected || sessionId !== sid) return;
    try {
      const ops = await pollOps();
      const theirs = ops.filter((o) => !ownOpSeqs.current.has(o.seq));
      const last = theirs[theirs.length - 1];
      if (last) setRemoteEdit(describeRemoteOp(last));
    } catch {
      // A failed poll is already visible in the connection bar; a second
      // banner saying the same thing would only crowd the timeline.
    }
  }, [connected, sessionId, sid, pollOps]);

  useFocusEffect(
    useCallback(() => {
      // Only while this screen is in front AND the app is foregrounded. iOS
      // freezes timers on suspend anyway, but an interval left running behind
      // a modal would keep spending the rate-limit budget on nothing.
      const timer = setInterval(() => {
        if (AppState.currentState === "active") void watchOps();
      }, OPS_POLL_MS);
      return () => clearInterval(timer);
    }, [watchOps]),
  );

  useAnnounce(error ?? remoteEdit);

  // -- edits ----------------------------------------------------------------

  const run = useCallback(
    async (tool: string, args: Record<string, unknown>) => {
      setError(null);
      try {
        const response = await dispatch(tool, args);
        if (response.op) {
          // Keep the set from growing across a long session; only the last
          // handful can still be unseen by `pollOps`.
          if (ownOpSeqs.current.size > 64) ownOpSeqs.current.clear();
          ownOpSeqs.current.add(response.op.seq);
        }
        setSelectedClipId(null);
      } catch (e) {
        if (!isCancellation(e)) setError(errorMessage(e));
      }
    },
    [dispatch],
  );

  const selected = selectedClipId === null ? null : clips.find((c) => c.id === selectedClipId) ?? null;
  const splitTarget = clipAt(clips, playhead);
  const lastOp = session?.ops[session.ops.length - 1] ?? null;
  const canUndo = ready && lastOp !== null && lastOp.tool !== "init";
  const canRedo = ready && session?.redo_available === true;
  const working = busy !== null;

  /** The track a clip sits on — `split_at` needs it, and it is not on the
   *  flattened strip clip because the strip only ever draws one lane. */
  const trackOfSelected = selectedClipId === null ? null : findClip(edl, selectedClipId)?.track.id ?? null;

  if (!sid) {
    return (
      <Screen kicker="Editor" title="No project" lede="Pick a project to open.">
        <Button label="Back to projects" onPress={() => router.replace("/projects")} />
      </Screen>
    );
  }

  return (
    <Screen
      kicker={session?.name ?? "Project"}
      title={hasVideo ? "Timeline" : "No video yet"}
      lede={
        hasVideo
          ? "Everything you change here is applied on your Mac, to the same project the Mac is showing."
          : "There is no video on this timeline yet. Add a clip from this phone, or from the Mac."
      }
      header={<ConnectionBar conn={conn} onRetry={() => void probe()} />}
      footer={
        <Row>
          <Button
            label="Add a clip"
            disabled={!connected || working}
            onPress={() => router.push({ pathname: "/import", params: { sid } })}
            style={{ flexGrow: 1 }}
          />
          <Button
            label="Export"
            variant="neutral"
            disabled={!connected || !hasVideo}
            onPress={() => router.push({ pathname: "/export", params: { sid } })}
          />
        </Row>
      }
    >
      {!connected && (
        <MacRequired message="The picture, the timeline and every edit come from your Mac. Nothing here is stored on the phone." />
      )}

      {error !== null && (
        <Note tone="danger" title="That edit did not go through">
          <View style={{ gap: space[2] }}>
            <Body tone="dim">{error}</Body>
            <Caption>
              Nothing was retried automatically — your Mac may have applied it before the connection
              dropped. The timeline below is what your Mac has now.
            </Caption>
          </View>
        </Note>
      )}

      {mediaWarning !== null && (
        <Note tone="warn" title="Video and thumbnails may not load">
          <View style={{ gap: space[2] }}>
            <Body tone="dim">{mediaWarning}</Body>
            <Caption>
              The timeline and every edit below still work — this only affects the pictures.
            </Caption>
          </View>
        </Note>
      )}

      {remoteEdit !== null && (
        <Note tone="info" title="The project changed while you were looking at it">
          <View style={{ gap: space[3] }}>
            <Body tone="dim">{remoteEdit}</Body>
            <Button label="Dismiss" variant="ghost" onPress={() => setRemoteEdit(null)} />
          </View>
        </Note>
      )}

      <PreviewPlayer
        controller={controller}
        source={preview}
        aspect={aspect}
        fps={fps}
        playhead={playhead}
        extent={extent}
        renderStatus={renderStatus}
        renderProgress={renderProgress}
        error={previewError}
        stale={stale}
        onRetry={() => void render(true)}
        emptyMessage={
          hasVideo ? null : "Add a clip and your Mac will render a preview of it here."
        }
      />

      {ready && hasVideo && (
        <TimelineStrip
          clips={clips}
          extent={extent}
          fps={fps}
          pixelsPerSecond={pixelsPerSecond}
          onZoomChange={(next) => setPixelsPerSecond(clampPixelsPerSecond(next))}
          playhead={playhead}
          selectedClipId={selectedClipId}
          onSelectClip={setSelectedClipId}
          onScrub={onScrub}
          client={client}
          sessionId={sid}
          mediaEnabled={mediaEnabled}
        />
      )}

      <Card>
        <Heading>Edit</Heading>
        <Row>
          <Button
            label="Undo"
            variant="neutral"
            disabled={!canUndo || working}
            hint={lastOp ? `Takes back: ${lastOp.summary}` : undefined}
            onPress={() => void run("undo", {})}
            style={{ flexGrow: 1 }}
          />
          <Button
            label="Redo"
            variant="neutral"
            disabled={!canRedo || working}
            onPress={() => void run("redo", {})}
            style={{ flexGrow: 1 }}
          />
        </Row>
        <Caption>
          {canUndo && lastOp
            ? `Undo takes back “${lastOp.summary}”. Your Mac and this phone share one undo stack, so that may be an edit made at the Mac.`
            : "Nothing to undo yet."}
        </Caption>
      </Card>

      <Card>
        <Heading>{selected === null ? "No clip selected" : basename(selected.src)}</Heading>
        {selected === null ? (
          <Body tone="dim">
            Tap a clip on the timeline to trim, split or remove it. The playhead decides where a
            split lands.
          </Body>
        ) : (
          <>
            <Row>
              <Stat value={timecode(selected.start)} label="Starts" />
              <Stat value={humanDuration(selected.duration)} label="Length" />
            </Row>
            <Row>
              <Button
                label="Split at the playhead"
                variant="neutral"
                disabled={!canSplit(selected, playhead) || working}
                onPress={() => void splitAt(selected, playhead, run, trackOfSelected)}
                style={{ flexGrow: 1 }}
              />
            </Row>
            <Row>
              <Button
                label="Remove and close the gap"
                variant="danger"
                disabled={working}
                onPress={() => void run("ripple_delete", { clip_id: selected.id })}
                style={{ flexGrow: 1 }}
              />
            </Row>
            {!canSplit(selected, playhead) && (
              <Caption>
                Move the playhead inside this clip to split it
                {splitTarget && splitTarget.id !== selected.id
                  ? ` — it is currently over ${basename(splitTarget.src)}.`
                  : "."}
              </Caption>
            )}
          </>
        )}
      </Card>

      {ready && (
        <Card tone="inset">
          <Heading>This project on your Mac</Heading>
          <Row>
            <Stat value={String(clips.length)} label="Video clips" />
            <Stat value={humanDuration(extent)} label="Length" />
            <Stat value={shortHash(edlHash)} label="State" />
          </Row>
          <Caption>
            {`Rendering, captions and every AI tool run on the Mac${
              conn.whoami ? ` — it runs ${conn.whoami.server.job_workers} job${conn.whoami.server.job_workers === 1 ? "" : "s"} at a time, shared with whoever is sitting at it` : ""
            }.`}
          </Caption>
          <Row>
            <Button
              label="AI tools"
              variant="ghost"
              onPress={() => router.push({ pathname: "/ai", params: { sid } })}
            />
            <Button
              label="Ask Claude"
              variant="ghost"
              onPress={() => router.push({ pathname: "/chat", params: { sid } })}
            />
          </Row>
        </Card>
      )}
    </Screen>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * `split_at` cuts every clip on a track that CONTAINS the time, so a playhead
 * on either edge splits nothing and the Mac would answer with an op that
 * changed nothing. Half-open on the left edge, and short of the right edge by
 * a hair so the second half is not empty.
 */
function canSplit(clip: StripClip, playhead: number): boolean {
  return playhead > clip.start + 1e-3 && playhead < clip.start + clip.duration - 1e-3;
}

async function splitAt(
  clip: StripClip,
  playhead: number,
  run: (tool: string, args: Record<string, unknown>) => Promise<void>,
  trackId: string | null,
): Promise<void> {
  if (!trackId || !canSplit(clip, playhead)) return;
  // Both arguments are required by the schema (`agent/tools.py:88`); sending
  // only the time silently splits nothing.
  await run("split_at", { track: trackId, time: playhead });
}

/** What to say when an op the phone did not make arrives. `Op.by` is only ever
 *  "user" or "claude" (`edl/ops_log.py:16`) — there is no phone-vs-Mac marker
 *  in the log, so the copy does not pretend to know which human it was. */
function describeRemoteOp(op: Op): string {
  const who = op.by === "claude" ? "Claude" : "Someone at the Mac, or this phone";
  return `${who} changed the project: ${op.summary}. The timeline below has been refreshed.`;
}

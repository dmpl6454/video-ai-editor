/**
 * Export: ask the Mac to render, watch it, bring the file back.
 *
 * FOUR THINGS THIS SCREEN REFUSES TO PRETEND.
 *
 * 1. It cannot render. Every byte of the video is produced by ffmpeg on the
 *    Mac; the phone starts a job and copies a file. The lede says so, and if
 *    the link is down the whole screen collapses to `MacRequired` rather than
 *    offering buttons that would fail.
 *
 * 2. It does not know how long this will take, and it does not guess. There is
 *    no "about 2 minutes" here — the render time depends on the Mac's CPU, the
 *    clip count and what else that Mac is doing, and a wrong estimate is worse
 *    than none. What it CAN state is true and useful: how many render jobs the
 *    Mac runs at once (`whoami.server.job_workers`, usually two, SHARED with
 *    whoever is sitting at the Mac), and how many jobs are ahead of this one.
 *    A job sitting in `queued` at 0% is the single most misread state in the
 *    product, so `queued` gets its own words and its own progress treatment.
 *
 * 3. It does not know the file size until the file starts arriving. The size
 *    shown during the download is measured, not modelled, and when the Mac
 *    sends no Content-Length the bar goes indeterminate instead of inventing a
 *    denominator.
 *
 * 4. Cancel means cancel. Export is a `cancellable` job on the Mac
 *    (`render_export` takes a `cancel_event`), so Cancel really does stop the
 *    render rather than just stopping us watching it.
 *
 * SUSPENSION. iOS suspends this app the moment it is backgrounded, taking
 * every timer with it, and `api/jobs.py` prunes finished jobs. A twelve-minute
 * export that succeeded in the user's pocket must not come back as a timeout,
 * so: keep-awake is held while a render is running, `runJob` discounts
 * suspended time from its own clock (`lib/jobs.ts`), and an `AppState` return
 * to `active` re-polls the job once and decides from `job.status` rather than
 * resuming a stopwatch.
 */

import { router } from "expo-router";
import { activateKeepAwakeAsync, deactivateKeepAwake } from "expo-keep-awake";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState, Share, View } from "react-native";

import {
  Badge,
  Body,
  Button,
  Caption,
  Card,
  Chip,
  Divider,
  Heading,
  MacRequired,
  Mono,
  Note,
  Progress,
  Row,
  Screen,
  Stat,
  Type,
  useAnnounce,
} from "../components/ui";
import { color, space } from "../constants/theme";
import { errorMessage, isCancellation } from "../lib/errors";
import {
  DEFAULT_EXPORT_OPTIONS,
  describeOptions,
  downloadExport,
  exportRequestBody,
  HEIGHT_CHOICES,
  QUALITY_CHOICES,
  saveToPhotos,
  type DownloadedExport,
  type ExportContainer,
  type ExportOptions,
  type QualityId,
} from "../lib/exportSave";
import { humanBytes, humanDuration, shortHash } from "../lib/format";
import { jobStatusLine, runJob, type JobProgress } from "../lib/jobs";
import { useStore } from "../lib/store";

/**
 * Give up WATCHING a render after this much observed time (time the app spent
 * suspended does not count — see `lib/jobs.ts`). Deliberately generous: a long
 * 4K export on a busy Mac is a real thing. It exists because `runJob` with no
 * `maxMs` waits forever, and a spinner with no end is not a state a user can
 * get out of. Hitting it does not stop the Mac — the message says so.
 */
const EXPORT_MAX_MS = 45 * 60_000;

/** What the screen is doing. Each one has exactly one set of controls. */
type Phase = "idle" | "rendering" | "downloading" | "ready" | "failed";

interface Finished extends DownloadedExport {
  /** The EDL hash the render was made from, so a later edit can mark it stale. */
  edlHash: string;
}

export default function Export() {
  const client = useStore((s) => s.client);
  const conn = useStore((s) => s.conn);
  const sessionId = useStore((s) => s.sessionId);
  const session = useStore((s) => s.session);
  const edl = useStore((s) => s.edl);

  const connected = conn.status === "connected";
  const workers = conn.whoami?.server.job_workers ?? null;

  const [options, setOptions] = useState<ExportOptions>(DEFAULT_EXPORT_OPTIONS);
  const [phase, setPhase] = useState<Phase>("idle");
  const [job, setJob] = useState<JobProgress | null>(null);
  const [downloaded, setDownloaded] = useState<number | null>(null);
  const [downloadTotal, setDownloadTotal] = useState<number | null>(null);
  const [finished, setFinished] = useState<Finished | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedToPhotos, setSavedToPhotos] = useState(false);

  const abortRef = useRef<{ aborted: boolean }>({ aborted: false });
  const downloadAbortRef = useRef<AbortController | null>(null);
  const jobIdRef = useRef<string | null>(null);

  const busy = phase === "rendering" || phase === "downloading";
  const currentHash = session?.summary.edl_hash ?? null;
  const duration = edl?.duration ?? session?.summary.duration ?? 0;
  const canvas = edl?.canvas ?? session?.summary.canvas ?? null;

  /**
   * The render is of the timeline as it was when the job started. If the EDL
   * has moved on since, the file on the phone is a picture of an older cut —
   * the desktop marks that with a struck-through link, and so does this.
   */
  const stale = finished !== null && currentHash !== null && finished.edlHash !== currentHash;

  /**
   * Leaving this modal stops the work it started.
   *
   * There was no unmount cleanup at all, and `runJob` was called with no
   * `maxMs` — which `lib/jobs.ts` documents as "wait indefinitely". So swiping
   * the modal down mid-render left the async `start()` continuation polling
   * `GET /api/jobs/{id}` every 700 ms for the life of the process, plus a jobs
   * listing on every poll while queued: ~1.4-2.9 requests a second of
   * foreground radio traffic against a 60 rps/IP limiter, competing with the
   * timeline's own filmstrip and ops poll, with no UI attached to any of it —
   * and then a multi-hundred-megabyte download and `setPhase` on an unmounted
   * component.
   *
   * Cancelling on unmount is the honest behaviour for a screen whose Cancel
   * really cancels the Mac's render: the alternative is a background job the
   * user cannot see, cannot cancel, and did not ask to keep. If a render should
   * ever survive leaving the screen, the watch belongs in `lib/store.ts` (which
   * already owns `busy`/`abort`) rather than in a detached closure here.
   */
  useEffect(() => {
    const abort = abortRef.current;
    return () => {
      abort.aborted = true;
      downloadAbortRef.current?.abort();
    };
  }, []);

  // Keep the screen awake only while the Mac is actually working for us.
  // Holding it for the whole visit would drain a phone sitting on a desk.
  useEffect(() => {
    if (!busy) return;
    void activateKeepAwakeAsync("vae-export");
    return () => {
      void deactivateKeepAwake("vae-export");
    };
  }, [busy]);

  // Coming back from the background: ask once, decide from the job's own
  // status. Never resume a stopwatch — see the header.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") return;
      const id = jobIdRef.current;
      if (!id || !client || phase !== "rendering") return;
      void client
        .getJob(id)
        .then((j) => setJob((p) => (p ? { ...p, status: j.status, progress: j.progress } : p)))
        .catch(() => undefined);
    });
    return () => sub.remove();
  }, [client, phase]);

  const statusLine = useMemo(() => {
    if (phase === "rendering" && job) return jobStatusLine(job);
    if (phase === "downloading") {
      if (downloadTotal === null) return "Copying the finished video to this iPhone…";
      return `Copying to this iPhone — ${humanBytes(downloaded)} of ${humanBytes(downloadTotal)}.`;
    }
    if (phase === "ready" && finished) return `Saved in this app — ${humanBytes(finished.size)}.`;
    if (phase === "failed" && error) return error;
    return null;
  }, [phase, job, downloadTotal, downloaded, finished, error]);

  useAnnounce(statusLine);

  const start = useCallback(async () => {
    if (!client || !sessionId) return;
    const abort = { aborted: false };
    abortRef.current = abort;
    jobIdRef.current = null;
    setError(null);
    setFinished(null);
    setSavedToPhotos(false);
    setDownloaded(null);
    setDownloadTotal(null);
    setJob(null);
    setPhase("rendering");

    try {
      // `wait=0` always. The synchronous branch renders on a request worker
      // and a multi-minute export would hit the client timeout long before
      // ffmpeg finished, leaving a render running with nobody watching it.
      const { job_id } = await client.request<{ job_id: string }>(
        "POST",
        `/api/sessions/${sessionId}/export?wait=0`,
        { body: exportRequestBody(options) },
      );
      jobIdRef.current = job_id;

      const outcome = await runJob(job_id, {
        getJob: (id) => client.getJob(id),
        getQueueDepth: (id) => client.queueDepthFor(sessionId, id),
        reportsProgress: true, // render_export takes on_progress; ffmpeg reports real frames
        // OBSERVED time, so hours in a pocket do not count (lib/jobs.ts). A
        // ceiling exists at all because "wait indefinitely" is not a state a
        // user can ever get out of, and the timeout branch below says the
        // honest thing — go and look at the Mac — rather than "it failed".
        maxMs: EXPORT_MAX_MS,
        signal: abort,
        onProgress: setJob,
      });

      if (outcome.kind === "cancelled") {
        setPhase("idle");
        return;
      }
      if (outcome.kind === "failed") throw new Error(outcome.message);
      if (outcome.kind === "timeout") {
        throw new Error("Your Mac is still rendering this. Leave the app open and check back.");
      }
      if (outcome.kind === "vanished") {
        // The Mac prunes finished jobs. Almost always this is a render that
        // COMPLETED while the phone was asleep, so the honest message is "go
        // and look", not "it failed".
        throw new Error(
          "Your Mac no longer has a record of that render. It may well have finished — check the Mac, or export again.",
        );
      }

      const result = outcome.result as { url?: string; filename?: string } | null;
      if (!result?.url || !result.filename) {
        throw new Error("Your Mac finished the render but did not say where the file is.");
      }

      setPhase("downloading");
      const controller = new AbortController();
      downloadAbortRef.current = controller;
      const file = await downloadExport({
        // Relative from the Mac (`main.py::_export_payload`); `mediaUrl` makes
        // it absolute and adds the short-lived `?k=` media token.
        url: await client.mediaUrl(result.url),
        filename: result.filename,
        headers: client.mediaHeaders(),
        signal: controller.signal,
        onProgress: (r) => {
          setDownloaded(r.bytesWritten);
          setDownloadTotal(r.totalBytes);
        },
      });

      setFinished({ ...file, edlHash: currentHash ?? "" });
      setPhase("ready");
    } catch (e) {
      if (isCancellation(e)) {
        setPhase("idle");
        return;
      }
      setError(errorMessage(e));
      setPhase("failed");
    } finally {
      downloadAbortRef.current = null;
    }
  }, [client, sessionId, options, currentHash]);

  const cancel = useCallback(() => {
    abortRef.current.aborted = true;
    downloadAbortRef.current?.abort();
    const id = jobIdRef.current;
    // Export IS cancellable on the Mac — `render_export` takes a cancel_event —
    // so this stops the render, not just our watching of it. A refusal is not
    // worth a banner: the local abort has already stopped us.
    if (id && client) void client.cancelJob(id).catch(() => undefined);
  }, [client]);

  const onSaveToPhotos = useCallback(async () => {
    if (!finished) return;
    setSaving(true);
    setError(null);
    try {
      await saveToPhotos(finished.uri);
      setSavedToPhotos(true);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }, [finished]);

  const onShare = useCallback(() => {
    if (!finished) return;
    // iOS takes a file:// url here and offers AirDrop, Messages, Files and
    // every app that accepts video. It is the escape hatch for anyone who does
    // not want a 4 GB render in their photo library.
    void Share.share({ url: finished.uri, title: finished.filename }).catch(() => undefined);
  }, [finished]);

  if (!connected) {
    return (
      <Screen
        kicker="Export"
        title="Render and save"
        lede="Rendering happens on your Mac."
        header={<TopStrip onClose={() => router.back()} />}
        inset="none"
      >
        <MacRequired message="Your Mac renders the video with ffmpeg and this phone copies the finished file across. Reconnect and the export controls come back." />
        <Button label="Go to connection settings" variant="neutral" onPress={() => router.push("/connect")} />
      </Screen>
    );
  }

  if (!sessionId) {
    return (
      <Screen
        kicker="Export"
        title="Render and save"
        header={<TopStrip onClose={() => router.back()} />}
        inset="none"
      >
        <Note tone="warn" title="No project open">
          Open a project first — an export is a render of one timeline.
        </Note>
      </Screen>
    );
  }

  return (
    <Screen
      kicker="Export"
      title="Render and save"
      lede="Your Mac renders this with ffmpeg. Nothing is uploaded anywhere, and nothing is rendered on the phone."
      header={<TopStrip onClose={() => router.back()} />}
      inset="none"
      footer={
        busy ? (
          <Button label="Cancel" variant="danger" onPress={cancel} hint="Stops the render on your Mac." />
        ) : (
          <Button
            label={phase === "failed" ? "Try again" : "Render on my Mac"}
            disabled={duration <= 0}
            onPress={() => void start()}
            hint={duration <= 0 ? "There is nothing on the timeline yet." : describeOptions(options)}
          />
        )
      }
    >
      <Card>
        <Heading>What gets rendered</Heading>
        <Row>
          <Stat value={humanDuration(duration)} label="Length" />
          <Stat value={canvas ? `${canvas.w}×${canvas.h}` : "—"} label="Canvas" />
          <Stat value={canvas ? `${canvas.fps} fps` : "—"} label="Frame rate" />
        </Row>
        <Caption>
          {`The whole timeline, flattened — ${session?.name ?? "this project"}, at edit ${shortHash(currentHash)}.`}
        </Caption>
      </Card>

      <Card>
        <Heading>Size</Heading>
        <Row accessibilityRole="radiogroup" accessibilityLabel="Export height">
          {HEIGHT_CHOICES.map((h) => (
            <Chip
              key={h ?? "source"}
              kind="choice"
              label={h === null ? "Source" : `${h}p`}
              selected={options.height === h}
              disabled={busy}
              onPress={() => setOptions((o) => ({ ...o, height: h }))}
            />
          ))}
        </Row>
        <Caption>
          {options.height === null
            ? "Renders at the canvas size above. Anything smaller re-scales on the Mac."
            : `Scaled to ${options.height} pixels tall, keeping the canvas shape.`}
        </Caption>

        <Divider />

        <Heading>Quality</Heading>
        <Row accessibilityRole="radiogroup" accessibilityLabel="Export quality">
          {QUALITY_CHOICES.map((q) => (
            <Chip
              key={q.id}
              kind="choice"
              label={q.label}
              selected={options.crf === q.crf}
              disabled={busy}
              onPress={() => setOptions((o) => ({ ...o, crf: q.crf }))}
            />
          ))}
        </Row>
        <Caption>{qualityNote(options.crf)}</Caption>

        <Divider />

        <Heading>File type</Heading>
        <Row accessibilityRole="radiogroup" accessibilityLabel="Container">
          {(["mp4", "mov"] as ExportContainer[]).map((c) => (
            <Chip
              key={c}
              kind="choice"
              label={c.toUpperCase()}
              sub={c === "mp4" ? "goes everywhere" : "for other editors"}
              selected={options.container === c}
              disabled={busy}
              onPress={() => setOptions((o) => ({ ...o, container: c }))}
            />
          ))}
        </Row>
        <Caption>
          The final size is only known once the file exists — this app measures it rather than
          guessing.
        </Caption>
      </Card>

      {busy && (
        <Card>
          <Row style={{ justifyContent: "space-between" }}>
            <Heading>{phase === "rendering" ? "Rendering on your Mac" : "Copying to this iPhone"}</Heading>
            {job?.status === "queued" && <Badge tone="warn" label="Queued" />}
          </Row>
          <Progress
            fraction={progressFraction(phase, job, downloaded, downloadTotal)}
            text={statusLine ?? "Working…"}
          />
          <Body tone="dim" accessibilityLiveRegion="polite">
            {statusLine}
          </Body>
          {phase === "rendering" && workers !== null && (
            <Caption>
              {`That Mac runs ${workers} render job${workers === 1 ? "" : "s"} at a time, shared with whoever is sitting at it. A queued export starts as soon as one frees up.`}
            </Caption>
          )}
        </Card>
      )}

      {phase === "failed" && error !== null && (
        <Note tone="danger" title="The export did not finish">
          {error}
        </Note>
      )}

      {phase === "ready" && finished !== null && (
        <Card>
          <Row style={{ justifyContent: "space-between" }}>
            <Heading>Ready</Heading>
            {stale ? <Badge tone="warn" label="Older edit" /> : <Badge tone="good" label="Current edit" />}
          </Row>
          <Mono>{finished.filename}</Mono>
          <Row>
            <Stat value={humanBytes(finished.size)} label="On disk" tone="good" />
            <Stat value={describeOptions(options)} label="Rendered as" />
          </Row>
          {stale && (
            <Note tone="warn" title="The timeline has changed since this render">
              This file is the cut as it was at edit {shortHash(finished.edlHash)}. Render again for
              an up-to-date one.
            </Note>
          )}
          <Button
            label={savedToPhotos ? "Saved to Photos" : "Save to Photos"}
            busy={saving}
            disabled={savedToPhotos}
            onPress={() => void onSaveToPhotos()}
            hint="Adds the video to your photo library. This app never reads your library."
          />
          <Button label="Share…" variant="neutral" onPress={onShare} hint="AirDrop, Messages, Files, or any app that takes video." />
          {error !== null && <Note tone="danger">{error}</Note>}
          <Caption>
            The copy on this iPhone lives in the app's cache, which iOS may clear when storage runs
            low. Save it to Photos or send it somewhere to keep it. The original stays on your Mac.
          </Caption>
        </Card>
      )}
    </Screen>
  );
}

/** The bar. Queued is deliberately indeterminate: a queued job's progress is a
 *  truthful 0 that reads as a hang, and an unmoving 0% bar is the single most
 *  misread thing in this product. */
function progressFraction(
  phase: Phase,
  job: JobProgress | null,
  downloaded: number | null,
  total: number | null,
): number | null {
  if (phase === "downloading") {
    if (total === null || total <= 0 || downloaded === null) return null;
    return Math.min(1, downloaded / total);
  }
  if (job === null || job.status === "queued") return null;
  return job.progress;
}

function qualityNote(crf: number): string {
  const q = QUALITY_CHOICES.find((c) => c.crf === crf);
  return q ? q.note : "";
}

/** A modal presentation gets no native header, so it brings its own. */
function TopStrip({ onClose }: { onClose: () => void }) {
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        justifyContent: "space-between",
        paddingHorizontal: space[5],
        paddingVertical: space[3],
        backgroundColor: color.bg1,
        borderBottomWidth: 1,
        borderBottomColor: color.line,
      }}
    >
      <Type variant="label" tone="dim">
        Export
      </Type>
      <Button label="Done" variant="ghost" onPress={onClose} />
    </View>
  );
}

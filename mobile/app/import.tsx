/**
 * Sending a clip from this phone's library to the Mac.
 *
 * THIS SCREEN IS DELIBERATELY SLOW TO COMMIT. Everything else in the app is a
 * request that either works or does not; this one moves hundreds of megabytes
 * over Wi-Fi and the user is standing there while it happens. So it shows what
 * it is about to send before it sends it, refuses in advance anything the Mac
 * would reject at the end, measures the speed rather than guessing it, and
 * keeps the screen awake so iOS does not suspend the transfer.
 *
 * THREE THINGS THE PICKER DOES THAT THE COPY HAS TO BE HONEST ABOUT:
 *   - iOS hands over ITS OWN COPY of the clip. For an HEVC original, a slow-mo
 *     recording, or anything in iCloud, that copy is often re-encoded, so the
 *     size here can differ from what the Photos app shows. Claiming "the
 *     original, untouched" would be a lie iOS makes for us.
 *   - `asset.fileSize` is frequently missing, so the size is measured off the
 *     copy on disk (`lib/upload.ts`), and the bar goes indeterminate rather
 *     than showing a confident 0 % when even that fails.
 *   - `asset.duration` is milliseconds while every field on the Mac is
 *     seconds. `millisToSeconds` is the only conversion, and it is unit-tested.
 *
 * The upload itself is `expo-file-system`'s native task, which is the only API
 * here that reports real progress. Everything about the REQUEST — the field
 * name, the two form flags, how a non-2xx body becomes a sentence — lives in
 * `lib/upload.ts` so it can be tested without a device.
 */

import { File, UploadType } from "expo-file-system";
import * as ImagePicker from "expo-image-picker";
import { activateKeepAwakeAsync, deactivateKeepAwake } from "expo-keep-awake";
import { router, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { View } from "react-native";

import { ConnectionBar } from "../components/ConnectionBar";
import {
  Body,
  Button,
  Caption,
  Card,
  Chip,
  Heading,
  MacRequired,
  Note,
  Progress,
  Row,
  Screen,
  Stat,
  useAnnounce,
} from "../components/ui";
import { space } from "../constants/theme";
import { errorMessage, isCancellation } from "../lib/errors";
import { humanBytes, humanDuration } from "../lib/format";
import { selectMediaEnabled, useStore } from "../lib/store";
import {
  describePickedAsset,
  uploadClip,
  uploadRefusal,
  uploadTiming,
  uploadTimingLine,
  type ClipToUpload,
  type NativeUploadResult,
  type UploadTaskSpec,
  type UploadTiming,
} from "../lib/upload";

/** Keeps the display on for the length of the transfer. iOS suspends a
 *  foreground app's JS the moment the screen locks, and a half-sent multipart
 *  body is a partial file the Mac then has to delete. */
const KEEP_AWAKE_TAG = "vae-upload";

export default function Import() {
  const { sid } = useLocalSearchParams<{ sid?: string }>();
  const conn = useStore((s) => s.conn);
  const client = useStore((s) => s.client);
  const probe = useStore((s) => s.probe);
  const refreshEdl = useStore((s) => s.refreshEdl);
  const mediaEnabled = useStore(selectMediaEnabled);
  const connected = conn.status === "connected";

  const [clip, setClip] = useState<ClipToUpload | null>(null);
  const [addToTimeline, setAddToTimeline] = useState(true);
  const [transcribe, setTranscribe] = useState(true);
  const [timing, setTiming] = useState<UploadTiming | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const uploading = timing !== null;
  const abort = useRef<AbortController | null>(null);
  // The native task's rejection for a user abort is not reliably an AbortError
  // by name, so the intent is recorded here rather than inferred from it. A
  // stop the user asked for must never be shown as a failure.
  const stopped = useRef(false);

  const maxUploadBytes = conn.whoami?.server.max_upload_bytes ?? null;
  const refusal = clip === null ? null : uploadRefusal(clip, maxUploadBytes);

  useAnnounce(error ?? done);

  // Never leave the wake lock behind — an unmount mid-upload (a back swipe, a
  // navigation) would otherwise hold the screen on until the app is killed.
  useEffect(() => {
    return () => {
      abort.current?.abort();
      void deactivateKeepAwake(KEEP_AWAKE_TAG).catch(() => undefined);
    };
  }, []);

  const pick = useCallback(async () => {
    setError(null);
    setDone(null);
    try {
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ["videos"],
        allowsMultipleSelection: false,
        // No EXIF: this app never reads location or camera metadata, and the
        // privacy manifest in app.json says so.
        exif: false,
      });
      if (result.canceled) return;
      const asset = result.assets[0];
      if (!asset) return;
      // `asset.fileSize` is missing more often than not; the file on disk is
      // the copy actually being uploaded, so measure that.
      const measured = safeFileSize(asset.uri);
      setClip(describePickedAsset(asset, measured));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  const send = useCallback(async () => {
    if (!client || !clip || !sid || refusal !== null) return;
    setError(null);
    setDone(null);
    setTiming(uploadTiming(0, clip.sizeBytes ?? 0, 0));

    const controller = new AbortController();
    abort.current = controller;
    stopped.current = false;
    await activateKeepAwakeAsync(KEEP_AWAKE_TAG).catch(() => undefined);

    try {
      const response = await uploadClip(
        (spec) => startNativeUpload(clip.uri, spec, controller.signal),
        client.baseUrl,
        {
          sid,
          clip,
          addToTimeline,
          transcribe,
          // The upload is not a media path, so it needs the bearer AND the
          // client header that forces the preflight a browser cannot pass.
          authHeaders: { ...client.mediaHeaders(), "X-VAE-Client": "1" },
          onProgress: setTiming,
        },
      );
      await refreshEdl();
      setDone(
        response.transcript_pending
          ? `Added ${clip.name}. Your Mac is transcribing it in the background — captions become available when that finishes.`
          : `Added ${clip.name}.`,
      );
      setClip(null);
    } catch (e) {
      if (!stopped.current && !isCancellation(e)) setError(errorMessage(e));
    } finally {
      abort.current = null;
      setTiming(null);
      void deactivateKeepAwake(KEEP_AWAKE_TAG).catch(() => undefined);
    }
  }, [client, clip, sid, refusal, addToTimeline, transcribe, refreshEdl]);

  if (!sid) {
    return (
      <Screen kicker="Import" title="No project open" lede="Open a project first, then add a clip to it.">
        <Button label="Back to projects" onPress={() => router.replace("/projects")} />
      </Screen>
    );
  }

  return (
    <Screen
      kicker="Import"
      title="Add a clip"
      lede="The clip is uploaded to your Mac, normalised there, and added to this project."
      header={<ConnectionBar conn={conn} onRetry={() => void probe()} />}
      footer={
        <View style={{ gap: space[2] }}>
          {uploading && timing !== null && (
            <>
              <Progress fraction={timing.fraction} text={uploadTimingLine(timing)} />
              <Caption>{uploadTimingLine(timing)}</Caption>
            </>
          )}
          <Row>
            <Button
              label={uploading ? "Uploading…" : "Send to the Mac"}
              busy={uploading}
              disabled={!connected || clip === null || refusal !== null || uploading}
              onPress={() => void send()}
              style={{ flexGrow: 1 }}
            />
            {uploading && (
              <Button
                label="Stop"
                variant="ghost"
                onPress={() => {
                  stopped.current = true;
                  abort.current?.abort();
                }}
              />
            )}
          </Row>
        </View>
      }
    >
      {!connected && (
        <MacRequired message="Uploading needs your Mac awake, on the same Wi-Fi, and running Video AI Editor." />
      )}

      {error !== null && (
        <Note tone="danger" title="The upload did not finish">
          {error}
        </Note>
      )}

      {done !== null && (
        <Note tone="good" title="Done">
          <View style={{ gap: space[3] }}>
            <Body tone="dim">{done}</Body>
            <Button
              label="Back to the timeline"
              variant="neutral"
              onPress={() => router.replace({ pathname: "/edit", params: { sid } })}
            />
          </View>
        </Note>
      )}

      <Card>
        <Heading>Pick a clip</Heading>
        <Body tone="dim">
          iPhone hands this app its own copy of whatever you choose, and for HEVC, slow-motion or
          iCloud clips that copy is often re-encoded — so the size below can differ from what Photos
          shows. It is the copy that gets uploaded.
        </Body>
        <Button
          label={clip === null ? "Choose from my library" : "Choose a different clip"}
          variant={clip === null ? "primary" : "neutral"}
          disabled={uploading || !mediaEnabled}
          onPress={() => void pick()}
        />
        {!mediaEnabled && <Caption>Connect to your Mac first — there is nowhere to send a clip yet.</Caption>}
      </Card>

      {clip !== null && (
        <Card>
          <Heading numberOfLines={2}>{clip.name}</Heading>
          <Row>
            <Stat value={humanBytes(clip.sizeBytes)} label="Size" />
            <Stat
              value={clip.durationSeconds === null ? "—" : humanDuration(clip.durationSeconds)}
              label="Length"
            />
          </Row>
          {refusal !== null ? (
            <Note tone="danger" title="Your Mac will not accept this">
              {refusal}
            </Note>
          ) : (
            <Caption>
              {maxUploadBytes === null
                ? "Over Wi-Fi this takes about a minute per gigabyte on a good network."
                : `Your Mac accepts uploads up to ${humanBytes(maxUploadBytes)}.`}
            </Caption>
          )}
          <Row>
            <Chip
              label="Add to the timeline"
              sub={addToTimeline ? "after the last clip" : "media bin only"}
              selected={addToTimeline}
              disabled={uploading}
              onPress={() => setAddToTimeline((v) => !v)}
            />
            <Chip
              label="Transcribe on the Mac"
              sub={transcribe ? "after the upload" : "skipped"}
              selected={transcribe}
              disabled={uploading}
              onPress={() => setTranscribe((v) => !v)}
            />
          </Row>
          <Caption>
            Transcription runs on the Mac after the upload returns, so the clip appears immediately
            and captions become available a little later.
          </Caption>
        </Card>
      )}
    </Screen>
  );
}

// ---------------------------------------------------------------------------
// The native transport
// ---------------------------------------------------------------------------

/**
 * The one call into `expo-file-system`. Everything about the request contract
 * is decided in `lib/upload.ts`; this only turns that spec into a task.
 *
 * `sessionType: 'background'` lets iOS keep the transfer going if the app is
 * suspended mid-upload. The JS task is not restored if the app is TERMINATED,
 * which is exactly why the screen also holds a wake lock — between the two,
 * a long upload survives the phone being put down.
 */
async function startNativeUpload(
  uri: string,
  spec: UploadTaskSpec,
  signal: AbortSignal,
): Promise<NativeUploadResult> {
  const file = new File(uri);
  const task = file.createUploadTask(spec.url, {
    httpMethod: "POST",
    uploadType: UploadType.MULTIPART,
    fieldName: spec.fieldName,
    mimeType: spec.mimeType,
    parameters: spec.parameters,
    headers: spec.headers,
    sessionType: "background",
    signal,
    onProgress: ({ bytesSent, totalBytes }) => spec.onProgress({ bytesSent, totalBytes }),
  });
  const result = await task.uploadAsync();
  return { status: result.status, body: result.body };
}

/** `File.size` is 0 for a file that cannot be read, and the constructor throws
 *  on a URI iOS will not hand us. Either way the answer is "we do not know",
 *  which the size formatter renders as an em dash rather than a zero. */
function safeFileSize(uri: string): number | null {
  try {
    const size = new File(uri).size;
    return size > 0 ? size : null;
  } catch {
    return null;
  }
}

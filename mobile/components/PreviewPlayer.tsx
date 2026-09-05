/**
 * The preview surface: a video frame, a transport, and — just as important —
 * an honest overlay for every state in which there is nothing to play.
 *
 * WHY THE PLAYER IS DRIVEN FROM A HOOK. The editor screen has to be able to
 * seek from the timeline strip and read the playhead back out of the player,
 * so a component that hid the `VideoPlayer` inside itself would need a ref and
 * an imperative handle to do the same job worse. `usePreviewPlayer` owns the
 * instance and the event subscriptions; `<PreviewPlayer>` is the view.
 *
 * WHY THE SOURCE IS REPLACED IN AN EFFECT rather than passed to
 * `useVideoPlayer`. There is nothing to play when the screen mounts: the URL
 * comes back from `lib/preview.ts` only after the Mac has finished a render
 * job. Handing `useVideoPlayer` a URL for an unrendered hash would trigger the
 * synchronous render path on the Mac — see that file's header for what that
 * costs. `replaceAsync` also keeps the asset load off the UI thread, which
 * `replace` explicitly does not on iOS.
 *
 * WHY `useCaching` IS OFF. The preview file changes every time the EDL does,
 * and the URL carries a short-lived media token in its query — so a cache
 * keyed on the URL would hold entries that can never be reused and would very
 * happily serve a stale cut after an edit.
 *
 * WHY THERE IS A RE-SIGN PATH. An mp4 is not fetched once. AVPlayer issues
 * successive HTTP Range requests as playback advances and re-requests on every
 * seek, all against the ONE stored URL — and the `?k=` token in that URL lives
 * sixty seconds (`api/pairing.py::MEDIA_TOKEN_TTL_S`). So playing past the
 * first minute, or scrubbing back after a pause, authenticated with an expired
 * credential and the range request 401'd. The source is deliberately replaced
 * only when `edlHash` changes, so token renewal cannot reset playback — which
 * left nothing at all to notice the expiry. `onExpired` closes that: on a
 * player error with a source still mounted, re-sign the path and reload at the
 * position the user was at. Bounded, because a genuine decode failure must not
 * become an infinite reload loop.
 */

// Deep import, not `{ Ionicons } from "@expo/vector-icons"`. The package root
// re-exports every family it ships, so Metro adds all seventeen font files —
// about 4 MB of MaterialCommunityIcons and Fontisto this app never draws — to
// the bundle's asset graph. This one import is 390 KB.
import Ionicons from "@expo/vector-icons/Ionicons";
import { useEvent } from "expo";
import { useVideoPlayer, VideoView, type VideoPlayer } from "expo-video";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { ActivityIndicator, Pressable, View } from "react-native";

import { color, HIT_SLOP_MIN, radius, space, useReducedMotion } from "../constants/theme";
import { timecode, timecodeFrames } from "../lib/format";
import type { PreviewSource } from "../lib/preview";
import { Body, Button, Caption, Note, Progress, Timecode, Type } from "./ui";

/** How often the player reports its position. 200 ms is five updates a second
 *  — enough that the playhead tracks the picture, few enough that it does not
 *  re-render the timeline strip on every frame. */
const TIME_UPDATE_INTERVAL_S = 0.2;

/** `StyleSheet.absoluteFill` is a registered style ID, not an object, so it
 *  cannot be spread into a style literal. */
const ABSOLUTE_FILL = { position: "absolute", left: 0, right: 0, top: 0, bottom: 0 } as const;

export interface PreviewController {
  player: VideoPlayer;
  /** Seconds, from the player's own clock. */
  currentTime: number;
  isPlaying: boolean;
  /** `loading` covers both the network fetch and the first decode. */
  status: "idle" | "loading" | "readyToPlay" | "error";
  togglePlay: () => void;
  pause: () => void;
  seekTo: (seconds: number) => void;
  stepFrames: (frames: number, fps: number) => void;
}

/**
 * Own a `VideoPlayer` bound to a preview URL.
 *
 * `headers` is applied ALONGSIDE the media token already in the URL. Neither
 * can be verified from this machine — there is no simulator here — so the app
 * sends both and `client.probeMedia()` is what tells the user which half broke
 * if one of them does.
 */
/** How many times a single source may be re-signed after a player error
 *  before we stop and let the overlay say so. Two covers "the token aged out"
 *  and "it aged out again while the phone was in a pocket"; more than that is
 *  not a token problem. */
const MAX_RESIGNS_PER_SOURCE = 2;

export function usePreviewPlayer(
  source: PreviewSource | null,
  headers: Record<string, string>,
  /** Re-sign `source.path` into a fresh absolute URL. Omit and the player
   *  simply keeps its original URL, which is the pre-0.6.0 behaviour. */
  onExpired?: (path: string) => Promise<string>,
): PreviewController {
  const player = useVideoPlayer(null, (p) => {
    p.timeUpdateEventInterval = TIME_UPDATE_INTERVAL_S;
    p.loop = false;
    // Editing is a frame-accurate activity; the default tolerance would let a
    // seek land on the nearest keyframe, which on a long GOP is half a second
    // away from the cut the user is looking at.
    p.seekTolerance = { toleranceBefore: 0, toleranceAfter: 0 };
  });

  // The URL carries a media token that is renewed as it ages, so comparing the
  // whole string would replace the source — and reset playback to zero — every
  // sixty seconds. The rendered hash is the thing that actually changed.
  const loadedHash = useRef<string | null>(null);

  // Reset per source, not per error: a fresh render deserves its own budget.
  const resigns = useRef(0);

  useEffect(() => {
    if (!source) {
      if (loadedHash.current !== null) {
        loadedHash.current = null;
        player.replace(null);
      }
      return;
    }
    if (loadedHash.current === source.edlHash) return;
    loadedHash.current = source.edlHash;
    resigns.current = 0;
    void player.replaceAsync({ uri: source.url, headers, useCaching: false });
  }, [player, source, headers]);

  const timeEvent = useEvent(player, "timeUpdate", null);
  const playingEvent = useEvent(player, "playingChange", null);
  const statusEvent = useEvent(player, "statusChange", null);

  const currentTime = timeEvent?.currentTime ?? 0;
  const isPlaying = playingEvent?.isPlaying ?? false;
  const status = statusEvent?.status ?? "idle";

  // A mid-stream failure on a source that is still mounted. Overwhelmingly the
  // expired-token case described in the header — the player has been happily
  // range-requesting the same URL for longer than the token lives.
  useEffect(() => {
    if (status !== "error" || !source || !onExpired) return;
    if (resigns.current >= MAX_RESIGNS_PER_SOURCE) return;
    resigns.current += 1;

    let cancelled = false;
    const resumeAt = player.currentTime;
    const wasPlaying = player.playing;

    void onExpired(source.path)
      .then(async (uri) => {
        if (cancelled) return;
        await player.replaceAsync({ uri, headers, useCaching: false });
        if (cancelled) return;
        // Put the user back where they were. A reload that silently restarts a
        // five-minute preview at zero is barely better than the stall it fixes.
        player.currentTime = Math.max(0, resumeAt);
        if (wasPlaying) player.play();
      })
      .catch(() => {
        // The Mac would not mint a token, which is a connection problem the
        // ConnectionBar is already narrating. The overlay below still explains
        // the picture, because `status` stays "error".
      });

    return () => {
      cancelled = true;
    };
    // `player.currentTime` is read at effect time on purpose, not tracked.
  }, [status, source, onExpired, player, headers]);

  const seekTo = useCallback(
    (seconds: number) => {
      if (!Number.isFinite(seconds)) return;
      player.currentTime = Math.max(0, seconds);
    },
    [player],
  );

  const togglePlay = useCallback(() => {
    if (player.playing) player.pause();
    else player.play();
  }, [player]);

  const pause = useCallback(() => {
    if (player.playing) player.pause();
  }, [player]);

  const stepFrames = useCallback(
    (frames: number, fps: number) => {
      // Stepping while playing would fight the playhead; an editor pressing a
      // frame key means "hold still and move one frame".
      player.pause();
      const step = fps > 0 ? frames / fps : frames / 30;
      player.currentTime = Math.max(0, player.currentTime + step);
    },
    [player],
  );

  return {
    player,
    currentTime,
    isPlaying,
    status,
    togglePlay,
    pause,
    seekTo,
    stepFrames,
  };
}

export interface PreviewPlayerProps {
  controller: PreviewController;
  source: PreviewSource | null;
  /** Aspect ratio of the project canvas, so the plate matches the render. */
  aspect: number;
  fps: number;
  /** Where the transport thinks the playhead is (the strip drives this while
   *  the user scrubs, so it can lead the player by a few frames). */
  playhead: number;
  /** Total timeline seconds, for the readout's denominator. */
  extent: number;
  /** One line about a render in flight. Null when there is nothing to say. */
  renderStatus: string | null;
  renderProgress: number | null;
  /** A failure that stopped the preview existing at all. */
  error: string | null;
  /** True when the EDL has moved on from the render on screen. */
  stale: boolean;
  onRetry: () => void;
  /** Null when this project has no video to preview yet. */
  emptyMessage: string | null;
}

export function PreviewPlayer({
  controller,
  source,
  aspect,
  fps,
  playhead,
  extent,
  renderStatus,
  renderProgress,
  error,
  stale,
  onRetry,
  emptyMessage,
}: PreviewPlayerProps) {
  const { player, isPlaying, status, togglePlay, stepFrames } = controller;

  // A portrait 9:16 project would otherwise take the whole screen and leave no
  // room for the timeline, so the plate is capped at a comfortable height and
  // the picture letterboxes inside it — the same compromise the render makes.
  const plateAspect = useMemo(() => Math.max(0.6, Math.min(aspect, 2.4)), [aspect]);

  const showOverlay = source === null || error !== null || emptyMessage !== null;

  return (
    <View style={{ gap: space[3] }}>
      <View
        style={{
          aspectRatio: plateAspect,
          borderRadius: radius.md,
          overflow: "hidden",
          backgroundColor: "#000",
          borderWidth: 1,
          borderColor: color.line,
        }}
      >
        {source !== null && error === null && (
          <VideoView
            player={player}
            style={{ flex: 1 }}
            contentFit="contain"
            nativeControls={false}
            allowsPictureInPicture={false}
          />
        )}
        {showOverlay && (
          <PlateOverlay
            message={error ?? emptyMessage ?? renderStatus ?? "Preparing the preview…"}
            tone={error !== null ? "danger" : "neutral"}
            progress={error === null && emptyMessage === null ? renderProgress : undefined}
          />
        )}
        {!showOverlay && status === "loading" && (
          <View
            pointerEvents="none"
            style={{ position: "absolute", right: space[3], top: space[3] }}
            accessibilityElementsHidden
          >
            <ActivityIndicator color={color.text} size="small" />
          </View>
        )}
      </View>

      {error !== null && <Button label="Try the render again" variant="neutral" onPress={onRetry} />}

      {error === null && stale && (
        <Note tone="warn" title="This picture is one edit behind">
          <View style={{ gap: space[3] }}>
            <Body tone="dim">
              The project changed since your Mac rendered this. Re-render to see the current cut.
            </Body>
            <Button label="Re-render the preview" variant="neutral" onPress={onRetry} />
          </View>
        </Note>
      )}

      {error === null && emptyMessage === null && renderStatus !== null && (
        <View style={{ gap: space[1] }}>
          <Progress fraction={renderProgress} text={renderStatus} />
          <Caption accessibilityLiveRegion="polite">{renderStatus}</Caption>
        </View>
      )}

      <View style={{ flexDirection: "row", alignItems: "center", gap: space[2] }}>
        <TransportButton
          icon="play-back"
          label="Back one frame"
          onPress={() => stepFrames(-1, fps)}
          disabled={source === null}
        />
        <TransportButton
          icon={isPlaying ? "pause" : "play"}
          label={isPlaying ? "Pause" : "Play"}
          primary
          onPress={togglePlay}
          disabled={source === null}
        />
        <TransportButton
          icon="play-forward"
          label="Forward one frame"
          onPress={() => stepFrames(1, fps)}
          disabled={source === null}
        />
        <View style={{ flex: 1, alignItems: "flex-end", gap: 1 }}>
          <Timecode accessibilityLabel={`Playhead at ${timecode(playhead)}`}>
            {`${timecode(playhead)} / ${timecode(extent)}`}
          </Timecode>
          <Caption>{timecodeFrames(playhead, fps)}</Caption>
        </View>
      </View>
    </View>
  );
}

function PlateOverlay({
  message,
  tone,
  progress,
}: {
  message: string;
  tone: "neutral" | "danger";
  progress?: number | null;
}) {
  return (
    <View
      accessibilityLiveRegion="polite"
      style={{
        ...ABSOLUTE_FILL,
        alignItems: "center",
        justifyContent: "center",
        paddingHorizontal: space[5],
        gap: space[3],
        backgroundColor: color.scrim,
      }}
    >
      <Type variant="label" tone={tone === "danger" ? "danger" : "dim"} style={{ textAlign: "center" }}>
        {message}
      </Type>
      {progress !== undefined && (
        <View style={{ alignSelf: "stretch" }}>
          <Progress fraction={progress} text={message} />
        </View>
      )}
    </View>
  );
}

function TransportButton({
  icon,
  label,
  onPress,
  disabled = false,
  primary = false,
}: {
  icon: React.ComponentProps<typeof Ionicons>["name"];
  label: string;
  onPress: () => void;
  disabled?: boolean;
  primary?: boolean;
}) {
  const reduced = useReducedMotion();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      style={({ pressed }) => ({
        width: primary ? 60 : HIT_SLOP_MIN,
        height: HIT_SLOP_MIN,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: radius.sm,
        borderWidth: 1,
        borderColor: primary ? color.accent.primary : color.lineStrong,
        backgroundColor: primary ? color.accent.primary : pressed ? color.bg3 : color.bg2,
        opacity: disabled ? 0.4 : pressed ? 0.92 : 1,
        // transform + opacity only — the compositor runs both, so a transport
        // press stays crisp while the strip is decoding thumbnails.
        transform: pressed && !reduced ? [{ scale: 0.96 }] : [{ scale: 1 }],
      })}
    >
      <Ionicons name={icon} size={primary ? 22 : 18} color={primary ? color.accent.primaryInk : color.text} />
    </Pressable>
  );
}

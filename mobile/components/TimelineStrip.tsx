/**
 * The timeline: a filmstrip that scrolls under a fixed playhead.
 *
 * WHY THE PLAYHEAD IS FIXED AND THE FILM MOVES. On a desktop the playhead
 * travels across a wide canvas and the mouse can land on it precisely. On a
 * phone the finger IS the cursor and it covers the thing it is pointing at, so
 * every phone editor worth using pins the playhead to the centre and scrolls
 * the film beneath it. That also makes scrubbing a plain scroll gesture —
 * inertia, rubber-banding and VoiceOver's own scroll handling all come free,
 * and there is no custom pan responder to fight with the parent scroll view.
 *
 * WHY THE CONTENT IS PADDED BY HALF THE VIEWPORT. Time zero has to be able to
 * reach the centre line, and so does the last frame. Without the padding the
 * first and last few seconds of every project would be unreachable, which is
 * exactly where the cuts that matter usually are.
 *
 * WHAT PROTECTS THE MAC. `lib/timeline.ts` decides which frames to ask for and
 * caps the answer at 24 — the rate limiter buckets every `/thumb` together
 * because it keys on the path without its query. This component asks only for
 * what that function returns, only while the connection is live, and never
 * again for a source that has already been refused: `/thumb` answers 403 for
 * any path outside the session directory, and the tool dispatcher legitimately
 * puts external paths on the timeline, so that 403 is a permanent property of
 * the clip rather than something to retry.
 *
 * THE 403 REASONING ABOVE IS RIGHT AND USED TO BE APPLIED TO THE WRONG THINGS.
 * `expo-image`'s `onError` carries no status code, so a 401 from an EXPIRED
 * media token was recorded exactly like a permanent 403: the clip's `src` went
 * into `failedSrcs`, `visibleThumbTimes` then dropped every frame of that clip,
 * and the blacklist only cleared on a change of session or connection — so one
 * stale token greyed out a whole clip's filmstrip for the rest of the visit.
 * Two changes keep the good reasoning and drop the bad: signed URLs now expire
 * out of the cache with their token (so a revisited tile re-signs instead of
 * replaying a dead credential), and a failure is only made permanent after
 * `client.probeMedia` confirms the Mac really is refusing that path.
 */

// Deep import, not `{ Ionicons } from "@expo/vector-icons"`. The package root
// re-exports every family it ships, so Metro adds all seventeen font files —
// about 4 MB of MaterialCommunityIcons and Fontisto this app never draws — to
// the bundle's asset graph. This one import is 390 KB.
import Ionicons from "@expo/vector-icons/Ionicons";
import { Image } from "expo-image";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  Pressable,
  ScrollView,
  View,
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";

import { color, HIT_SLOP_MIN, radius, space } from "../constants/theme";
import type { ApiClient } from "../lib/api";
import { basename, timecode } from "../lib/format";
import {
  clampPixelsPerSecond,
  clipAt,
  MAX_PIXELS_PER_SECOND,
  MIN_PIXELS_PER_SECOND,
  thumbPath,
  tickStepSeconds,
  tickTimes,
  timeAtX,
  visibleThumbTimes,
  xForTime,
  type StripClip,
  type ThumbTile,
} from "../lib/timeline";
import { Caption, Timecode, Type } from "./ui";

/** Height of the film row. 96px thumbnails (`THUMB_HEIGHT_PX`) are twice this
 *  so they stay sharp on a 2× and 3× screen. */
const ROW_HEIGHT = 56;
const RULER_HEIGHT = 26;

/** One tap of the zoom control. 1.6× is a noticeable but not disorienting
 *  jump — four taps cross the whole range. */
const ZOOM_FACTOR = 1.6;

/** How far VoiceOver's increment/decrement moves the playhead. */
const A11Y_STEP_SECONDS = 1;

/** A seek smaller than this is noise from scroll settling, not an intention. */
const SEEK_EPSILON_S = 1 / 240;

/** How long the strip stops following the player after the user moves it. */
const FOLLOW_SUPPRESS_MS = 450;

/** Ceiling on the resolved-thumbnail-URL map. Each entry is a short string, but
 *  an afternoon of scrubbing a long project would otherwise grow it without
 *  limit; dropping the lot is cheap because the images stay in expo-image's
 *  own disk cache and only the signed URL has to be minted again. */
const MAX_CACHED_THUMB_URLS = 400;

/** Safety margin before a signed URL's own token deadline. A tile that starts
 *  loading just inside the boundary must still be accepted when the request
 *  actually reaches the Mac. */
const THUMB_URL_EXPIRY_MARGIN_MS = 5_000;

/** Fallback life for a signed URL when the client cannot say when its token
 *  dies (no token issued yet). Comfortably inside the Mac's 60 s TTL. */
const THUMB_URL_FALLBACK_TTL_MS = 30_000;

export interface TimelineStripProps {
  clips: readonly StripClip[];
  /** Total timeline seconds the strip can travel over. */
  extent: number;
  fps: number;
  pixelsPerSecond: number;
  onZoomChange: (next: number) => void;
  /** Seconds. Drives the scroll position while the user is not scrubbing. */
  playhead: number;
  selectedClipId: string | null;
  onSelectClip: (clipId: string | null) => void;
  /** Called while scrubbing, with the time under the centre line. */
  onScrub: (seconds: number) => void;
  /** Null until the phone is paired; media loaders stay off without it. */
  client: ApiClient | null;
  sessionId: string;
  /** False whenever the connection is anything but `connected`. */
  mediaEnabled: boolean;
}

export function TimelineStrip(props: TimelineStripProps) {
  const {
    clips,
    extent,
    pixelsPerSecond,
    onZoomChange,
    playhead,
    selectedClipId,
    onSelectClip,
    onScrub,
    client,
    sessionId,
    mediaEnabled,
  } = props;

  const pps = clampPixelsPerSecond(pixelsPerSecond);
  const scrollRef = useRef<ScrollView>(null);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [offsetX, setOffsetX] = useState(0);

  // The last position we scrolled to ourselves, so the echoed scroll event does
  // not read as a new seek and bounce straight back at the player.
  const selfScrolledTo = useRef<number | null>(null);
  // When the user last moved the strip. Following the player is suppressed for
  // a moment afterwards: a seek we sent takes ~200 ms to come back as a time
  // update, and correcting the scroll position with that stale value is what
  // turns a flick into a rubber band that snaps back under the finger.
  const userScrollAt = useRef(0);

  const contentWidth = xForTime(Math.max(extent, 0), pps);
  const halfViewport = viewportWidth / 2;

  // -- what to draw ---------------------------------------------------------

  const windowStart = timeAtX(offsetX, pps);
  const windowEnd = timeAtX(offsetX + Math.max(viewportWidth, 1), pps);

  const [failedSrcs, setFailedSrcs] = useState<ReadonlySet<string>>(() => new Set());
  // A source can start working again — the Mac may have been asleep, or the
  // project reopened after the file moved back — so the permanent-failure set
  // is scoped to one session and cleared when the media link comes back.
  useEffect(() => {
    setFailedSrcs((prev) => (prev.size === 0 ? prev : new Set()));
  }, [mediaEnabled, sessionId]);

  // A screen of lead-in and lead-out on each side: a scroll then reveals frames
  // and blocks that are already mounted, rather than a grey band that fills in
  // after the finger stops.
  const bufferStart = windowStart - (windowEnd - windowStart);
  const bufferEnd = windowEnd + (windowEnd - windowStart);

  /**
   * Only the clips near the viewport are mounted. A real project can carry
   * hundreds, and every one of them is an absolutely-positioned Pressable with
   * its own thumbnails — mounting the far end of a twenty-minute cut costs a
   * scroll frame for something nobody can see.
   */
  const visibleClips = useMemo(
    () =>
      viewportWidth === 0
        ? []
        : clips.filter((c) => c.start < bufferEnd && c.start + c.duration > bufferStart),
    [clips, bufferStart, bufferEnd, viewportWidth],
  );

  const tiles = useMemo(
    () =>
      viewportWidth === 0
        ? []
        : visibleThumbTimes({
            clips: visibleClips,
            windowStart: bufferStart,
            windowEnd: bufferEnd,
            pixelsPerSecond: pps,
            failedSrcs,
          }),
    [visibleClips, bufferStart, bufferEnd, pps, failedSrcs, viewportWidth],
  );

  const thumbUrls = useThumbUrls(tiles, client, sessionId, mediaEnabled);

  /**
   * A tile failed to load. Confirm WHY before writing the source off.
   *
   * `expo-image`'s `onError` carries no status, and the two failures it hides
   * mean opposite things. A 403 is permanent — `/thumb` refuses any src outside
   * the session directory, and `add_clip` legitimately puts `~/Movies/…` on the
   * timeline — while a 401 from an aged-out media token, or a dropped packet,
   * is over in a second. Treating them alike is what left a whole clip's
   * filmstrip grey for the rest of a session after one stale token.
   *
   * So the error triggers a single authenticated probe of the same path. Only
   * `forbidden` is recorded; everything else is left to retry on the next
   * paint. `probing` de-duplicates, because a filmstrip fails two dozen tiles
   * for one source at once and this must not become two dozen probes.
   */
  const probing = useRef<Set<string>>(new Set());

  const onThumbError = useCallback(
    (src: string, t: number) => {
      if (!client || probing.current.has(src)) return;
      probing.current.add(src);
      void client
        .probeMedia(thumbPath(sessionId, src, t))
        .then((result) => {
          if (result.ok || result.kind !== "forbidden") return;
          setFailedSrcs((prev) => {
            if (prev.has(src)) return prev;
            const next = new Set(prev);
            next.add(src);
            return next;
          });
        })
        .catch(() => undefined)
        .finally(() => {
          probing.current.delete(src);
        });
    },
    [client, sessionId],
  );

  // -- scroll ↔ playhead ----------------------------------------------------

  const seekFromOffset = useCallback(
    (x: number) => {
      const t = Math.min(Math.max(0, timeAtX(x, pps)), Math.max(0, extent));
      if (Math.abs(t - playhead) < SEEK_EPSILON_S) return;
      onScrub(t);
    },
    [pps, extent, playhead, onScrub],
  );

  const onScroll = useCallback(
    (e: NativeSyntheticEvent<NativeScrollEvent>) => {
      const x = e.nativeEvent.contentOffset.x;
      setOffsetX(x);
      // The echo of our own `scrollTo` is not a scrub and must not seek.
      if (selfScrolledTo.current !== null && Math.abs(selfScrolledTo.current - x) < 1) return;
      selfScrolledTo.current = null;
      userScrollAt.current = Date.now();
      seekFromOffset(x);
    },
    [seekFromOffset],
  );

  // Follow the player. Skipped for a moment after the user's own scroll, and
  // skipped when the strip is already where it should be — `scrollTo` is not
  // free and calling it every 200 ms would cancel the user's own momentum.
  useEffect(() => {
    if (viewportWidth === 0) return;
    if (Date.now() - userScrollAt.current < FOLLOW_SUPPRESS_MS) return;
    const target = xForTime(playhead, pps);
    if (Math.abs(target - offsetX) < 0.5) return;
    selfScrolledTo.current = target;
    scrollRef.current?.scrollTo({ x: target, animated: false });
  }, [playhead, pps, viewportWidth, offsetX]);

  const onAccessibilityAction = useCallback(
    (event: { nativeEvent: { actionName: string } }) => {
      const dir = event.nativeEvent.actionName === "increment" ? 1 : -1;
      const next = Math.min(Math.max(0, playhead + dir * A11Y_STEP_SECONDS), Math.max(0, extent));
      onScrub(next);
    },
    [playhead, extent, onScrub],
  );

  const selected = selectedClipId === null ? null : clips.find((c) => c.id === selectedClipId) ?? null;
  const underPlayhead = clipAt(clips, playhead);

  return (
    <View style={{ gap: space[2] }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: space[2] }}>
        <Type variant="kicker" style={{ flex: 1, textTransform: "uppercase" }}>
          Timeline
        </Type>
        <ZoomButton
          icon="remove"
          label="Zoom out"
          disabled={pps <= MIN_PIXELS_PER_SECOND}
          onPress={() => onZoomChange(pps / ZOOM_FACTOR)}
        />
        <ZoomButton
          icon="add"
          label="Zoom in"
          disabled={pps >= MAX_PIXELS_PER_SECOND}
          onPress={() => onZoomChange(pps * ZOOM_FACTOR)}
        />
      </View>

      <View
        onLayout={(e: LayoutChangeEvent) => setViewportWidth(e.nativeEvent.layout.width)}
        accessible
        accessibilityRole="adjustable"
        accessibilityLabel="Timeline scrubber"
        accessibilityValue={{ text: `${timecode(playhead)} of ${timecode(extent)}` }}
        accessibilityHint="Swipe up or down to move the playhead one second."
        accessibilityActions={[{ name: "increment" }, { name: "decrement" }]}
        onAccessibilityAction={onAccessibilityAction}
        style={{
          height: RULER_HEIGHT + ROW_HEIGHT,
          borderRadius: radius.sm,
          overflow: "hidden",
          backgroundColor: color.bg1,
          borderWidth: 1,
          borderColor: color.line,
        }}
      >
        <ScrollView
          ref={scrollRef}
          horizontal
          showsHorizontalScrollIndicator={false}
          scrollEventThrottle={32}
          onScroll={onScroll}
          onScrollBeginDrag={() => {
            userScrollAt.current = Date.now();
          }}
          contentContainerStyle={{ paddingHorizontal: halfViewport }}
        >
          <View style={{ width: Math.max(contentWidth, 1) }}>
            <Ruler
              windowStart={windowStart}
              windowEnd={windowEnd}
              pixelsPerSecond={pps}
              contentWidth={contentWidth}
            />
            <View style={{ height: ROW_HEIGHT, justifyContent: "center" }}>
              {visibleClips.map((clip) => (
                <ClipBlock
                  key={clip.id}
                  clip={clip}
                  pixelsPerSecond={pps}
                  selected={clip.id === selectedClipId}
                  tiles={tiles.filter((t) => t.clipId === clip.id)}
                  thumbUrls={thumbUrls}
                  onError={onThumbError}
                  onPress={() => onSelectClip(clip.id === selectedClipId ? null : clip.id)}
                />
              ))}
            </View>
          </View>
        </ScrollView>

        <Playhead x={halfViewport} />
      </View>

      <View style={{ flexDirection: "row", alignItems: "center", gap: space[3] }}>
        <Timecode style={{ minWidth: 76 }}>{timecode(playhead)}</Timecode>
        <Caption style={{ flex: 1 }} numberOfLines={1}>
          {describeSelection(selected, underPlayhead)}
        </Caption>
      </View>
    </View>
  );
}

/** What the line under the strip says. Names the clip rather than its id —
 *  `c_8f3a…` tells an editor nothing about which shot they are looking at. */
function describeSelection(selected: StripClip | null, underPlayhead: StripClip | null): string {
  if (selected) return `Selected: ${basename(selected.src)}`;
  if (underPlayhead) return `Under the playhead: ${basename(underPlayhead.src)}`;
  return "Tap a clip to select it.";
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function Playhead({ x }: { x: number }) {
  return (
    <View
      pointerEvents="none"
      accessibilityElementsHidden
      style={{ position: "absolute", left: x - 1, top: 0, bottom: 0, width: 2, alignItems: "center" }}
    >
      <View style={{ flex: 1, width: 2, backgroundColor: color.selection }} />
      <View
        style={{
          position: "absolute",
          top: 0,
          width: 10,
          height: 10,
          borderRadius: 5,
          backgroundColor: color.selection,
        }}
      />
    </View>
  );
}

function Ruler({
  windowStart,
  windowEnd,
  pixelsPerSecond,
  contentWidth,
}: {
  windowStart: number;
  windowEnd: number;
  pixelsPerSecond: number;
  contentWidth: number;
}) {
  const step = tickStepSeconds(pixelsPerSecond);
  const times = tickTimes(Math.max(0, windowStart), windowEnd, step);
  return (
    <View
      accessibilityElementsHidden
      style={{
        height: RULER_HEIGHT,
        width: Math.max(contentWidth, 1),
        borderBottomWidth: 1,
        borderBottomColor: color.line,
      }}
    >
      {times.map((t) => (
        <View key={t} style={{ position: "absolute", left: xForTime(t, pixelsPerSecond), top: 0, bottom: 0 }}>
          <View style={{ width: 1, height: 6, backgroundColor: color.lineStrong }} />
          <Type variant="kicker" tone="dim" style={{ marginLeft: 3, marginTop: 1 }}>
            {timecode(t)}
          </Type>
        </View>
      ))}
    </View>
  );
}

function ClipBlock({
  clip,
  pixelsPerSecond,
  selected,
  tiles,
  thumbUrls,
  onError,
  onPress,
}: {
  clip: StripClip;
  pixelsPerSecond: number;
  selected: boolean;
  tiles: ThumbTile[];
  thumbUrls: ReadonlyMap<string, string>;
  /** `t` is the tile's timestamp, so the handler can probe the exact path
   *  that failed rather than guessing one. */
  onError: (src: string, t: number) => void;
  onPress: () => void;
}) {
  const left = xForTime(clip.start, pixelsPerSecond);
  const width = Math.max(3, xForTime(clip.duration, pixelsPerSecond));
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Clip ${basename(clip.src)}, ${timecode(clip.duration)} long, starting at ${timecode(clip.start)}`}
      accessibilityState={{ selected }}
      style={({ pressed }) => ({
        position: "absolute",
        left,
        width,
        height: ROW_HEIGHT - 10,
        borderRadius: radius.xs,
        overflow: "hidden",
        // The lane colour is the base coat. It shows through wherever a
        // thumbnail has not arrived — or can never arrive — so a clip is
        // always visible as a block even when its frames are not.
        backgroundColor: pressed ? color.bg3 : color.track.video,
        opacity: pressed ? 0.9 : 1,
        borderWidth: selected ? 2 : 1,
        borderColor: selected ? color.selection : color.bg0,
      })}
    >
      {tiles.map((tile) => {
        const uri = thumbUrls.get(tile.key);
        if (!uri) return null;
        return (
          <Image
            key={tile.key}
            source={{ uri }}
            style={{ position: "absolute", left: tile.x - left, top: 0, width: tile.width, height: ROW_HEIGHT - 10 }}
            contentFit="cover"
            transition={0}
            cachePolicy="memory-disk"
            onError={() => onError(tile.src, tile.t)}
            accessible={false}
          />
        );
      })}
    </Pressable>
  );
}

function ZoomButton({
  icon,
  label,
  onPress,
  disabled,
}: {
  icon: React.ComponentProps<typeof Ionicons>["name"];
  label: string;
  onPress: () => void;
  disabled: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      style={({ pressed }) => ({
        width: HIT_SLOP_MIN,
        height: 34,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: radius.sm,
        borderWidth: 1,
        borderColor: color.lineStrong,
        backgroundColor: pressed ? color.bg3 : color.bg2,
        opacity: disabled ? 0.4 : 1,
      })}
    >
      <Ionicons name={icon} size={16} color={color.text} />
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Thumbnail URLs
// ---------------------------------------------------------------------------

/**
 * Resolve tiles to loadable URLs.
 *
 * The media token is minted asynchronously and shared by every tile — the
 * client collapses concurrent callers onto one mint, which matters because a
 * strip asks for two dozen at once. Resolved URLs are kept across renders so a
 * scroll back over ground already covered costs nothing, and the map is
 * cleared the moment media is disabled so a token that belongs to a dropped
 * connection can never be handed to a loader.
 */
interface CachedThumbUrl {
  url: string;
  /** Epoch ms after which this URL's `?k=` token will be refused. */
  expiresAt: number;
}

function useThumbUrls(
  tiles: readonly ThumbTile[],
  client: ApiClient | null,
  sessionId: string,
  mediaEnabled: boolean,
): ReadonlyMap<string, string> {
  // The cache lives in a ref, not in state. As state it would have to be a
  // dependency of the effect that fills it, and every fill would re-run the
  // effect — which, while the strip is being scrolled and `tiles` is changing
  // anyway, discards in-flight work and asks for the same URLs again.
  //
  // Entries carry a DEADLINE because the `?k=` token inside them lives sixty
  // seconds. Holding them for the session meant that scrolling to 04:00 and
  // back to 00:30 two minutes later replayed dead tokens: anything expo-image
  // had evicted from memory refetched and got a 401.
  const urls = useRef<Map<string, CachedThumbUrl>>(new Map());
  const resolving = useRef<Set<string>>(new Set());
  const mounted = useRef(true);
  const [redrawCount, redraw] = useReducer((n: number) => n + 1, 0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // A token belongs to one credential and one connection. When either changes,
  // every URL we are holding is a URL that will now be refused.
  useEffect(() => {
    if (urls.current.size === 0 && resolving.current.size === 0) return;
    urls.current.clear();
    resolving.current.clear();
    redraw();
  }, [mediaEnabled, sessionId, client]);

  useEffect(() => {
    if (!mediaEnabled || !client) return;
    const now = Date.now();
    // Drop everything whose token has died (or is about to) before deciding
    // what is missing, so an expired tile is re-signed rather than reused.
    let evicted = false;
    for (const [key, entry] of urls.current) {
      if (entry.expiresAt <= now) {
        urls.current.delete(key);
        evicted = true;
      }
    }

    const wanted = tiles.filter((t) => !urls.current.has(t.key) && !resolving.current.has(t.key));
    if (wanted.length === 0) {
      if (evicted) redraw();
      return;
    }
    for (const t of wanted) resolving.current.add(t.key);

    void Promise.all(
      wanted.map(async (t) => {
        const url = await client.mediaUrl(thumbPath(sessionId, t.src, t.t));
        // Read the deadline AFTER minting: `mediaUrl` may have renewed the
        // token, and the URL is only good for as long as the token inside it.
        const tokenExpiry = client.mediaTokenExpiresAt();
        const expiresAt =
          (tokenExpiry ?? Date.now() + THUMB_URL_FALLBACK_TTL_MS) - THUMB_URL_EXPIRY_MARGIN_MS;
        return [t.key, { url, expiresAt }] as const;
      }),
    )
      .then((pairs) => {
        if (!mounted.current) return;
        if (urls.current.size > MAX_CACHED_THUMB_URLS) urls.current.clear();
        for (const [key, entry] of pairs) urls.current.set(key, entry);
        redraw();
      })
      .catch(() => {
        // A token the Mac would not mint is a connection problem, and the
        // connection bar is already saying so. The tiles simply stay as the
        // lane colour until it comes back.
      })
      .finally(() => {
        for (const t of wanted) resolving.current.delete(t.key);
      });
  }, [tiles, client, sessionId, mediaEnabled]);

  // The view wants plain URLs; the deadlines are this hook's business.
  return useMemo(() => {
    const flat = new Map<string, string>();
    for (const [key, entry] of urls.current) flat.set(key, entry.url);
    return flat;
    // `redrawCount` is the signal that `urls.current` changed — the ref itself
    // is stable, so it can never be the dependency.
  }, [redrawCount]);
}

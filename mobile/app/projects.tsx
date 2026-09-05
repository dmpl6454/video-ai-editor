/**
 * The project list: every session the Mac has, newest first.
 *
 * WHY POSTERS LOAD LAZILY AND ONE AT A TIME. `GET /api/sessions` is built from
 * `storage.py::list_sessions`, which reads each project's `meta.json` and the
 * directory's mtime and nothing else — there is no thumbnail in the response
 * and there never has been. A poster therefore costs one EDL fetch plus one
 * `/thumb` per project, and the rate limiter buckets every `/thumb` together
 * because it keys on the path without its query. Firing those in parallel for
 * a dozen projects is how a list turns itself grey with 429s. So they are
 * fetched strictly one project at a time, in list order, and a project without
 * a poster is a perfectly good row rather than a hole waiting to be filled.
 *
 * WHY "Edited" AND NOT "Created". `created_at` is the directory's `st_mtime`
 * (`storage.py:91`), which moves every time the project is saved. Labelling it
 * "Created" would be wrong on every project anyone has ever touched twice.
 */

import { router } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, RefreshControl, View } from "react-native";

import { ConnectionBar } from "../components/ConnectionBar";
import {
  Body,
  Button,
  Caption,
  Card,
  Field,
  Heading,
  MacRequired,
  Note,
  Row,
  ScreenList,
  Type,
  useAnnounce,
} from "../components/ui";
import { color, radius, space } from "../constants/theme";
import { Image } from "expo-image";
import type { ApiClient } from "../lib/api";
import { posterSpec } from "../lib/edl";
import { errorMessage } from "../lib/errors";
import { basename, relativeTimeFromEpochSeconds } from "../lib/format";
import { thumbPath } from "../lib/timeline";
import { selectMediaEnabled, useStore } from "../lib/store";
import type { SessionSummaryRow } from "../lib/types";

export default function Projects() {
  const conn = useStore((s) => s.conn);
  const client = useStore((s) => s.client);
  const probe = useStore((s) => s.probe);
  const mediaEnabled = useStore(selectMediaEnabled);
  const connected = conn.status === "connected";

  const [sessions, setSessions] = useState<SessionSummaryRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);

  const refresh = useCallback(async () => {
    if (!client) return;
    setLoading(true);
    try {
      const { sessions: rows } = await client.listSessions();
      setSessions(rows);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    if (connected) void refresh();
  }, [connected, refresh]);

  useAnnounce(error);

  const create = useCallback(async () => {
    if (!client) return;
    setCreating(true);
    try {
      const created = await client.createSession(newName.trim() || undefined);
      setNewName("");
      router.push({ pathname: "/edit", params: { sid: created.id } });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setCreating(false);
    }
  }, [client, newName]);

  const remove = useCallback(
    (row: SessionSummaryRow) => {
      Alert.alert(
        `Delete “${row.name}”?`,
        "This deletes the project on your Mac — its timeline, its uploaded clips and every render. It cannot be undone from here.",
        [
          { text: "Keep it", style: "cancel" },
          {
            text: "Delete",
            style: "destructive",
            onPress: () => {
              if (!client) return;
              // Drop the row first: the Mac's delete is a directory removal
              // that has already happened by the time it answers, and a list
              // that keeps showing the project until a refetch lands reads as
              // "the delete did not work".
              setSessions((prev) => prev.filter((s) => s.id !== row.id));
              client
                .request("DELETE", `/api/sessions/${row.id}`)
                .catch((e: unknown) => {
                  setError(errorMessage(e));
                  void refresh();
                });
            },
          },
        ],
      );
    },
    [client, refresh],
  );

  // Only the rows the user can actually see. `GET /api/sessions` is
  // unpaginated, and the poster walk costs one EDL fetch plus one signed URL
  // per project — so on a Mac with 200 projects the old "fetch them all"
  // version spent 200 sequential round trips for a screen showing six, and
  // every URL minted after the first minute of that walk had already expired by
  // the time its row scrolled into view.
  const [visibleIds, setVisibleIds] = useState<readonly string[]>([]);
  const visibleSessions = useMemo(
    () => sessions.filter((s) => visibleIds.includes(s.id)),
    [sessions, visibleIds],
  );
  const posters = useProjectPosters(visibleSessions, client, mediaEnabled);

  const renderRow = useCallback(
    ({ item }: { item: SessionSummaryRow }) => (
      <ProjectRow
        row={item}
        posterUri={posters.get(item.id) ?? null}
        disabled={!connected}
        onOpen={() => router.push({ pathname: "/edit", params: { sid: item.id } })}
        onDelete={() => remove(item)}
      />
    ),
    [posters, connected, remove],
  );

  return (
    <ScreenList
      kicker="Video AI Editor"
      title="Projects"
      lede="Every project lives on your Mac. This list is what it has right now."
      header={<ConnectionBar conn={conn} onRetry={() => void probe()} />}
      data={sessions}
      keyExtractor={(row) => row.id}
      renderItem={renderRow}
      onVisibleChange={setVisibleIds}
      refreshControl={
        <RefreshControl
          refreshing={loading}
          onRefresh={() => void refresh()}
          tintColor={color.textDim}
          enabled={connected}
        />
      }
      above={
        <>
      {!connected && (
        <MacRequired message="Projects, clips and renders all live on your Mac. Connect to it to see them." />
      )}

      {error !== null && (
        <Note tone="danger" title="That did not work">
          {error}
        </Note>
      )}

      <Card>
        <Heading>Start something new</Heading>
        <Body tone="dim">
          A new project is empty until you add a clip. Naming it now is easier than finding
          “s_4f21c9” later.
        </Body>
        <Field
          label="Project name"
          value={newName}
          onChangeText={setNewName}
          placeholder="Untitled cut"
          autoCapitalize="sentences"
          returnKeyType="go"
          onSubmitEditing={() => void create()}
        />
        <Button
          label={creating ? "Creating…" : "New project"}
          busy={creating}
          disabled={!connected || creating}
          onPress={() => void create()}
        />
      </Card>

      {connected && sessions.length === 0 && !loading && (
        <Card tone="inset">
          <Heading>No projects yet</Heading>
          <Body tone="dim">
            Nothing on this Mac so far. Create one above, then add a clip from this phone’s library
            or from the Mac itself.
          </Body>
        </Card>
      )}
        </>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// One row
// ---------------------------------------------------------------------------

function ProjectRow({
  row,
  posterUri,
  disabled,
  onOpen,
  onDelete,
}: {
  row: SessionSummaryRow;
  posterUri: string | null;
  disabled: boolean;
  onOpen: () => void;
  onDelete: () => void;
}) {
  return (
    <Card>
      <View style={{ flexDirection: "row", gap: space[3], alignItems: "center" }}>
        <Poster uri={posterUri} />
        <View style={{ flex: 1, gap: 2 }}>
          <Type variant="heading" numberOfLines={2}>
            {row.name}
          </Type>
          <Caption>{`Edited ${relativeTimeFromEpochSeconds(row.created_at)}`}</Caption>
          {row.source ? <Caption numberOfLines={1}>{basename(row.source)}</Caption> : null}
        </View>
      </View>
      <Row>
        <Button label="Open" disabled={disabled} onPress={onOpen} style={{ flexGrow: 1 }} />
        <Button label="Delete" variant="ghost" disabled={disabled} onPress={onDelete} />
      </Row>
    </Card>
  );
}

const POSTER_W = 84;
const POSTER_H = 56;

/** A frame, or a plate that says nothing rather than pretending to be one. */
function Poster({ uri }: { uri: string | null }) {
  return (
    <View
      accessibilityElementsHidden
      style={{
        width: POSTER_W,
        height: POSTER_H,
        borderRadius: radius.xs,
        overflow: "hidden",
        backgroundColor: color.bg2,
        borderWidth: 1,
        borderColor: color.line,
      }}
    >
      {uri !== null && (
        <Image source={{ uri }} style={{ flex: 1 }} contentFit="cover" transition={120} cachePolicy="memory-disk" />
      )}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Posters
// ---------------------------------------------------------------------------

/**
 * Fetch one poster at a time, in list order, for the rows PASSED IN.
 *
 * The caller passes only what is on screen (`ScreenList`'s `onVisibleChange`),
 * which is the difference between six requests and two hundred on a real Mac —
 * and it also keeps every signed URL well inside the media token's sixty-second
 * life, because a URL is now minted a moment before its row is looked at rather
 * than minutes before it scrolls into view.
 *
 * A project with no video clip, or whose first clip's source lives outside the
 * session directory (`/thumb` answers 403 for those, by design), simply never
 * gets a poster — the row is complete without one, and asking again on every
 * refresh would spend the rate-limit budget proving the same thing.
 *
 * Successful posters DO expire, though: the `?k=` token inside the URL is only
 * good for sixty seconds, so a row scrolled away from and back to must re-sign
 * rather than hand `expo-image` a dead credential.
 */

/** Life of a signed poster URL before a re-visible row re-signs it. Inside the
 *  Mac's `MEDIA_TOKEN_TTL_S` (60 s) with room for the request itself. */
const POSTER_URL_TTL_MS = 45_000;

interface Poster {
  url: string;
  expiresAt: number;
}

function useProjectPosters(
  sessions: readonly SessionSummaryRow[],
  client: ApiClient | null,
  mediaEnabled: boolean,
): ReadonlyMap<string, string> {
  const [posters, setPosters] = useState<ReadonlyMap<string, Poster>>(() => new Map());
  /** A mirror of `posters` for the effect to read WITHOUT depending on it.
   *  As a dependency, every resolved poster would cancel the walk in progress
   *  and restart it — correct, but n restarts for n rows. The expiry check is
   *  re-run whenever the visible set changes, which is exactly when a row
   *  scrolls back into view and its URL matters again. */
  const postersRef = useRef<ReadonlyMap<string, Poster>>(posters);
  /** Ids we have already tried. A row here is never re-fetched EXCEPT when it
   *  holds a poster whose signed URL has aged out. */
  const attempted = useRef<Set<string>>(new Set());
  const running = useRef(false);

  useEffect(() => {
    if (!mediaEnabled) {
      // The token in every poster URL belongs to a connection that has gone.
      attempted.current.clear();
      postersRef.current = new Map();
      setPosters(postersRef.current);
    }
  }, [mediaEnabled, client]);

  useEffect(() => {
    if (!mediaEnabled || !client || running.current) return;
    const now = Date.now();
    const pending = sessions.filter((row) => {
      const held = postersRef.current.get(row.id);
      if (held) return held.expiresAt <= now;   // expired: re-sign it
      return !attempted.current.has(row.id);    // never tried: try once
    });
    if (pending.length === 0) return;

    let alive = true;
    running.current = true;

    void (async () => {
      for (const row of pending) {
        if (!alive) break;
        attempted.current.add(row.id);
        try {
          const spec = posterSpec(await client.getEdl(row.id));
          if (!spec) continue;
          const url = await client.mediaUrl(thumbPath(row.id, spec.src, spec.t));
          if (!alive) break;
          setPosters((prev) => {
            const next = new Map(prev).set(row.id, {
              url,
              expiresAt: Date.now() + POSTER_URL_TTL_MS,
            });
            postersRef.current = next;
            return next;
          });
        } catch {
          // A project whose EDL or first frame cannot be read is still a
          // perfectly usable row. There is nothing to tell the user here that
          // opening the project would not tell them better.
        }
      }
      running.current = false;
    })();

    return () => {
      alive = false;
      running.current = false;
    };
  }, [sessions, client, mediaEnabled]);

  // Callers want URLs; the deadlines are this hook's business.
  return useMemo(() => {
    const flat = new Map<string, string>();
    for (const [id, poster] of posters) flat.set(id, poster.url);
    return flat;
  }, [posters]);
}

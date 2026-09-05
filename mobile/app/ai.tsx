/**
 * The AI screen: every one of the Mac's tools that is worth a thumb.
 *
 * WHAT THIS SCREEN IS HONEST ABOUT, IN ORDER:
 *
 *   1. Nothing here runs on the phone. Every card dispatches to the Mac and
 *      waits; when the Mac is not there, the cards are not there either, and
 *      the reason is on screen rather than implied by a spinner.
 *   2. The Mac runs a fixed, small number of job workers, and they are shared
 *      with whoever is sitting at it. The count comes from `whoami` — it is not
 *      a guess — and it is why a job can sit at 0% for minutes without anything
 *      being wrong.
 *   3. Sixteen of the desktop's tools are not here, and the screen says which
 *      and why rather than letting the user hunt for them.
 *   4. A tool the Mac cannot run says which dependency is missing and prints
 *      the command that fixes it, verbatim.
 *
 * ONE TOOL AT A TIME. The store has a single `busy` slot because the Mac has a
 * single dispatch path per session, and two concurrent mutating dispatches on
 * one EDL is how you get an op log nobody can reason about. Every other Run
 * button says which tool it is waiting behind rather than going quietly grey.
 */

import { useCallback, useMemo, useState } from "react";
import { RefreshControl, View } from "react-native";

import {
  Body,
  Button,
  Caption,
  Card,
  Field,
  Heading,
  MacRequired,
  Note,
  Screen,
  Title,
  Type,
} from "../components/ui";
import { IDLE_RUN, ToolCard, type RunState } from "../components/ai/ToolCard";
import { useToolCatalog } from "../components/ai/useToolCatalog";
import { color, space } from "../constants/theme";
import {
  groupEntries,
  MAC_ONLY,
  MOBILE_CATALOG,
  visibleEntries,
  type CatalogEntry,
} from "../lib/catalog";
import { ApiError, errorMessage } from "../lib/errors";
import { selectConnectionLine, selectIsConnected, useStore } from "../lib/store";

export default function Ai() {
  const client = useStore((s) => s.client);
  const sessionId = useStore((s) => s.sessionId);
  const session = useStore((s) => s.session);
  const edl = useStore((s) => s.edl);
  const busy = useStore((s) => s.busy);
  const dispatch = useStore((s) => s.dispatch);
  const cancelBusy = useStore((s) => s.cancelBusy);
  const refreshEdl = useStore((s) => s.refreshEdl);
  const connected = useStore(selectIsConnected);
  const connectionLine = useStore(selectConnectionLine);
  const workers = useStore((s) => s.conn.whoami?.server.job_workers ?? null);

  const catalog = useToolCatalog(connected ? client : null);
  const [query, setQuery] = useState("");
  const [runs, setRuns] = useState<Record<string, RunState>>({});

  const toolsByName = useMemo(
    () => new Map((catalog.tools ?? []).map((t) => [t.name, t])),
    [catalog.tools],
  );
  const advertised = useMemo(
    () => (catalog.tools === null ? null : new Set(catalog.tools.map((t) => t.name))),
    [catalog.tools],
  );
  const groups = useMemo(
    () => groupEntries(visibleEntries(MOBILE_CATALOG, advertised, query)),
    [advertised, query],
  );

  const setRun = useCallback((tool: string, state: RunState) => {
    setRuns((r) => ({ ...r, [tool]: state }));
  }, []);

  /**
   * Run a tool through the store, which is the ONE mutation path in this app —
   * the same one a gesture on the Mac and a tool call from Claude go down — so
   * the op log, the undo stack and the EDL refresh all see it.
   *
   * `cancellable` and `reportsProgress` are read off the schema the Mac sent.
   * Guessing either is how a client ends up offering a Cancel button that does
   * nothing while the handler runs happily to completion.
   */
  const runTool = useCallback(
    async (entry: CatalogEntry, args: Record<string, unknown>) => {
      const schema = toolsByName.get(entry.tool);
      setRun(entry.tool, { status: "running" });
      try {
        const res = await dispatch(entry.tool, args, {
          cancellable: schema?.cancellable === true,
          reportsProgress: schema?.reports_progress === true,
        });
        setRun(entry.tool, { status: "done", result: res.result });
      } catch (e) {
        const kind = e instanceof ApiError ? e.kind : "network";
        setRun(entry.tool, { status: "error", message: errorMessage(e), kind });
      }
    },
    [dispatch, setRun, toolsByName],
  );

  // "Use this hook" runs the real `add_hook_overlay` entry rather than a
  // synthetic one, so the result lands on that card and reads exactly as it
  // would have if the user had filled the form in themselves.
  const hookEntry = useMemo(
    () => MOBILE_CATALOG.find((e) => e.tool === "add_hook_overlay") ?? null,
    [],
  );
  const useHook = useCallback(
    (text: string) => {
      if (hookEntry === null) return;
      void runTool(hookEntry, { text });
    },
    [hookEntry, runTool],
  );

  const reloadCatalog = catalog.reload;
  const refresh = useCallback(() => {
    reloadCatalog();
    // The EDL matters here too: the clip pickers are built from it, and a tool
    // that timed out on the phone may well have landed on the Mac since.
    void refreshEdl().catch(() => undefined);
  }, [reloadCatalog, refreshEdl]);

  return (
    <Screen
      kicker="Runs on your Mac"
      title="AI tools"
      lede={
        session === null
          ? "Every tool here uses your Mac's models, its ffmpeg and its disk."
          : `Every tool here runs on your Mac and edits “${session.name}”.`
      }
      refreshControl={
        <RefreshControl
          refreshing={catalog.loading}
          onRefresh={refresh}
          tintColor={color.textDim}
        />
      }
      footer={<Footer workers={workers} />}
    >
      {!connected && <MacRequired message={connectionLine} />}

      {connected && sessionId === null && (
        <Note tone="warn" title="No project open">
          Open a project first. These tools edit a timeline, and there is not one to edit yet.
        </Note>
      )}

      {connected && sessionId !== null && (
        <>
          <Field
            label="Search"
            value={query}
            onChangeText={setQuery}
            placeholder="captions, upscale, hook…"
            autoCapitalize="none"
            hint={`${MOBILE_CATALOG.length} tools on the phone, ${MAC_ONLY.length} more on the Mac.`}
          />

          <FeatureStatus
            summary={catalog.features?.summary ?? null}
            error={catalog.featuresError}
            loading={catalog.loading}
            onRetry={catalog.reload}
          />

          {catalog.toolsError !== null && (
            <Note tone="danger" title="Your Mac did not send its tool list">
              <View style={{ gap: space[3] }}>
                <Body tone="dim">{catalog.toolsError}</Body>
                <Button label="Try again" variant="neutral" onPress={catalog.reload} />
              </View>
            </Note>
          )}

          {catalog.tools === null && catalog.toolsError === null && (
            <Caption>Asking your Mac which tools it has…</Caption>
          )}

          {groups.map((g) => (
            <View key={g.group} style={{ gap: space[3] }}>
              <Title>{g.group}</Title>
              {g.entries.map((entry) => {
                const schema = toolsByName.get(entry.tool);
                if (schema === undefined) return null;
                return (
                  <ToolCard
                    key={entry.tool}
                    entry={entry}
                    schema={schema}
                    features={catalog.features}
                    featuresFailed={catalog.featuresError !== null}
                    edl={edl}
                    run={runs[entry.tool] ?? IDLE_RUN}
                    busy={busy}
                    onRun={(e, args) => void runTool(e, args)}
                    onCancel={() => void cancelBusy()}
                    onReset={(tool) => setRun(tool, IDLE_RUN)}
                    onUseHook={useHook}
                  />
                );
              })}
            </View>
          ))}

          {catalog.tools !== null && groups.length === 0 && (
            <Caption>
              {query.trim() === ""
                ? "Your Mac advertises none of these tools. It may be running an older build."
                : `Nothing matches “${query.trim()}”.`}
            </Caption>
          )}

          <MacOnlyCard />
        </>
      )}
    </Screen>
  );
}

function FeatureStatus({
  summary,
  error,
  loading,
  onRetry,
}: {
  summary: string | null;
  error: string | null;
  loading: boolean;
  onRetry: () => void;
}) {
  if (error !== null) {
    return (
      <Note tone="warn" title="Could not check optional features">
        <View style={{ gap: space[3] }}>
          <Body tone="dim">
            {`${error} Tools stay available — a missing dependency will be reported when you run one, rather than guessed at now.`}
          </Body>
          <Button label="Check again" variant="neutral" onPress={onRetry} />
        </View>
      </Note>
    );
  }
  if (summary === null) {
    return <Caption>{loading ? "Checking what your Mac has installed…" : "Not checked yet."}</Caption>;
  }
  return <Caption>{summary}</Caption>;
}

/**
 * The tools that stayed on the Mac. Collapsed, because it is reference rather
 * than a task — but present, because a user who goes looking for "Erase
 * object" deserves to find out where it went from the app rather than by
 * concluding the phone is broken.
 */
function MacOnlyCard() {
  const [open, setOpen] = useState(false);
  return (
    <Card tone="inset">
      <Heading>{`${MAC_ONLY.length} tools stayed on the Mac`}</Heading>
      <Body tone="dim">
        They need a file path, a box drawn on the picture, a colour picked off the frame, or a
        timeline you can scrub. None of those are things a phone does well.
      </Body>
      <Button
        label={open ? "Hide the list" : "Show the list"}
        variant="ghost"
        onPress={() => setOpen((o) => !o)}
        accessibilityLabel={open ? "Hide the list of Mac-only tools" : "Show the list of Mac-only tools"}
        style={{ alignSelf: "flex-start" }}
      />
      {open && (
        <View style={{ gap: space[3] }}>
          {MAC_ONLY.map((m) => (
            <View key={m.tool} style={{ gap: 2 }}>
              <Type variant="label">{m.label}</Type>
              <Caption>{m.why}</Caption>
            </View>
          ))}
        </View>
      )}
    </Card>
  );
}

function Footer({ workers }: { workers: number | null }) {
  return (
    <Caption>
      {workers === null
        ? "Nothing here runs on this phone. Every tool uses your Mac."
        : `Nothing here runs on this phone. Your Mac runs ${workers} job${workers === 1 ? "" : "s"} at a time, shared with whoever is sitting at it — that is why a job can wait before it starts.`}
    </Caption>
  );
}

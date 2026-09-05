/**
 * One tool: what it does, what it needs, and what happened when you ran it.
 *
 * THE CARD NEVER PROMISES WHAT THE BACKEND CANNOT DELIVER. `cancellable` and
 * `reports_progress` come from `/api/tools`, where the Mac derives them from
 * the handler's own signature — a tool with no `cancel_event` parameter keeps
 * running after a cancel and commits anyway. So the Cancel button appears only
 * when the schema says the handler can hear it, and the progress bar goes
 * indeterminate rather than sitting confidently at 0% when the handler takes
 * no `set_progress`. Neither flag is ever hardcoded here.
 *
 * A GATE IS AN EXPLANATION, NOT A GREY BUTTON. When `/api/features` says the
 * dependency is missing, the card says which feature, whether the packaged app
 * excludes it on purpose, and prints the Mac-side command verbatim. There is
 * no clipboard package in this app and there is no honest way to run a command
 * on the Mac from here, so the text is selectable and the copy says where it
 * goes. Pretending otherwise would be the worse option.
 *
 * A TIMEOUT IS NOT A FAILURE. Only the ten tools on the async list go through
 * the Mac's job queue; everything else is a synchronous dispatch with a
 * ninety-second ceiling. Hitting that ceiling means the phone stopped waiting,
 * not that the tool stopped working — the edit still lands. That outcome gets
 * the caution tone and its own sentence, because showing it in red next to the
 * word "failed" would send the user to look for a problem that is not there.
 */

import { useMemo, useState } from "react";
import { Pressable, View } from "react-native";

import { Badge, Body, Button, Caption, Card, Mono, Note, Progress, Row, Type, useAnnounce } from "../ui";
import { color, HIT_SLOP_MIN, radius, space, useReducedMotion } from "../../constants/theme";
import { resultSummary } from "../../lib/aiResults";
import { gateFor, runsAsJob, type CatalogEntry } from "../../lib/catalog";
import type { ApiErrorKind } from "../../lib/errors";
import { jobStatusLine } from "../../lib/jobs";
import { buildArgs, fieldsFor, initialValues } from "../../lib/schemaForm";
import type { BusyState } from "../../lib/store";
import type { EDL, FeatureReport, ToolSchema } from "../../lib/types";
import { ToolForm } from "./ToolForm";
import { ToolResult } from "./ToolResult";

export type RunState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; result: unknown }
  | { status: "error"; message: string; kind: ApiErrorKind };

export const IDLE_RUN: RunState = { status: "idle" };

export interface ToolCardProps {
  entry: CatalogEntry;
  schema: ToolSchema;
  features: FeatureReport | null;
  featuresFailed: boolean;
  edl: EDL | null;
  run: RunState;
  /** The store's single busy slot. Non-null means SOME tool is running — the
   *  Mac shares one dispatch path, so a second Run would queue behind it. */
  busy: BusyState | null;
  onRun: (entry: CatalogEntry, args: Record<string, unknown>) => void;
  onCancel: () => void;
  onReset: (tool: string) => void;
  onUseHook: (text: string) => void;
}

/**
 * What VoiceOver hears. Only transitions, never a ticking clock — a
 * three-minute run should be two or three announcements, not two hundred.
 * "Done" carries the handler's own summary, because "cut 12 silences" is the
 * sentence a person actually wanted and "done" is not.
 */
function announcement(entry: CatalogEntry, run: RunState): string | null {
  if (run.status === "running") return `${entry.label}: working on your Mac`;
  if (run.status === "done") return `${entry.label}: ${resultSummary(entry.tool, run.result, entry.label)}`;
  if (run.status === "error") return `${entry.label}: ${run.message}`;
  return null;
}

export function ToolCard(props: ToolCardProps) {
  const { entry, schema, features, featuresFailed, edl, run, busy, onRun, onCancel, onReset } = props;
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, unknown> | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const fields = useMemo(() => fieldsFor(schema, entry), [schema, entry]);
  const gate = gateFor(entry, features, values ?? undefined);
  const isJob = runsAsJob(entry);
  const running = run.status === "running";
  const otherRunning = busy !== null && !running;
  const noKey = entry.keyHint !== undefined && features !== null && !features.anthropic_key_set;

  useAnnounce(announcement(entry, run));

  const toggle = () => {
    if (!open && values === null) setValues(initialValues(fields));
    setOpen((o) => !o);
  };

  const change = (name: string, value: unknown) => {
    setValues((v) => ({ ...(v ?? {}), [name]: value }));
    setErrors((e) => {
      if (!(name in e)) return e;
      return Object.fromEntries(Object.entries(e).filter(([k]) => k !== name));
    });
  };

  const submit = () => {
    if (values === null) return;
    const built = buildArgs(fields, values);
    setErrors(built.errors);
    if (Object.keys(built.errors).length > 0) return;
    onRun(entry, built.args);
  };

  return (
    <Card>
      <CardHeader label={entry.label} open={open} onPress={toggle} />

      <Row>
        {isJob ? (
          <Badge tone="info" label="background job" />
        ) : (
          <Badge tone="good" label="quick" />
        )}
        {entry.readOnly === true && <Badge tone="good" label="changes nothing" />}
        {!gate.ok && <Badge tone="warn" label="not installed" />}
        {gate.ok && gate.checking && !featuresFailed && <Badge tone="info" label="checking" />}
        {noKey && <Badge tone="warn" label="no API key" />}
      </Row>

      <Body tone="dim">{entry.description}</Body>

      {entry.advanced !== undefined && <Caption>{entry.advanced}</Caption>}

      {!gate.ok && <GateNotice feature={gate.feature} fix={gate.fix} excluded={gate.packagedExcluded} />}

      {open && values !== null && (
        <View style={{ gap: space[4] }}>
          {noKey && entry.keyHint !== undefined ? <Note tone="warn">{entry.keyHint}</Note> : null}

          <ToolForm
            fields={fields}
            values={values}
            errors={errors}
            disabled={running || !gate.ok}
            edl={edl}
            onChange={change}
          />

          {run.status === "idle" && (
            <View style={{ gap: space[2] }}>
              <Button
                label={`Run ${entry.label.toLowerCase()}`}
                disabled={!gate.ok || otherRunning}
                onPress={submit}
              />
              {otherRunning && (
                <Caption>
                  {`Your Mac is busy with ${busy?.tool ?? "another tool"}. One at a time.`}
                </Caption>
              )}
            </View>
          )}

          {running && <RunningState busy={busy} entry={entry} onCancel={onCancel} />}

          {run.status === "done" && (
            <View style={{ gap: space[3] }}>
              {entry.readOnly === true ? (
                <ToolResult
                  tool={entry.tool}
                  label={entry.label}
                  result={run.result}
                  busy={busy !== null}
                  {...(entry.tool === "generate_hook" ? { onUseHook: props.onUseHook } : {})}
                />
              ) : (
                <Note tone="good" title="Done">
                  {`${resultSummary(entry.tool, run.result, entry.label)}. The change is on your Mac and in this project’s history.`}
                </Note>
              )}
              <Button
                label="Run it again"
                variant="ghost"
                onPress={() => onReset(entry.tool)}
                style={{ alignSelf: "flex-start" }}
              />
            </View>
          )}

          {run.status === "error" && (
            <View style={{ gap: space[3] }}>
              {run.kind === "timeout" ? (
                <Note tone="warn" title="Still running on your Mac">
                  {`${run.message} Nothing was lost — pull down on the project to see the result once it lands.`}
                </Note>
              ) : run.kind === "cancelled" ? (
                <Note tone="warn" title="Stopped">
                  {run.message}
                </Note>
              ) : (
                <Note tone="danger" title="That did not run">
                  {run.message}
                </Note>
              )}
              <Button
                label="Try again"
                variant="ghost"
                onPress={() => onReset(entry.tool)}
                style={{ alignSelf: "flex-start" }}
              />
            </View>
          )}
        </View>
      )}
    </Card>
  );
}

/**
 * The card's own disclosure control.
 *
 * Not the kit's `Button`: that one centres its label and stamps its own
 * `accessibilityState`, and a card head needs the title left-aligned, a
 * chevron that turns, and an honest `expanded` state for VoiceOver. The
 * pressed treatment is the kit's — a 2% scale and a lifted surface, on
 * `transform` and `opacity` only, and not at all under reduced motion.
 */
function CardHeader({ label, open, onPress }: { label: string; open: boolean; onPress: () => void }) {
  const reduced = useReducedMotion();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ expanded: open }}
      accessibilityHint={open ? "Collapses this tool" : "Opens its settings"}
      style={({ pressed }) => ({
        minHeight: HIT_SLOP_MIN,
        flexDirection: "row",
        alignItems: "center",
        gap: space[3],
        marginHorizontal: -space[2],
        paddingHorizontal: space[2],
        borderRadius: radius.sm,
        backgroundColor: pressed ? color.bg2 : "transparent",
        opacity: pressed ? 0.92 : 1,
        transform: pressed && !reduced ? [{ scale: 0.99 }] : [{ scale: 1 }],
      })}
    >
      <View style={{ flex: 1 }}>
        <Type variant="heading">{label}</Type>
      </View>
      <Type
        variant="heading"
        tone="dim"
        accessibilityElementsHidden
        importantForAccessibility="no"
        style={{ transform: [{ rotate: open ? "90deg" : "0deg" }] }}
      >
        ›
      </Type>
    </Pressable>
  );
}

function RunningState({
  busy,
  entry,
  onCancel,
}: {
  busy: BusyState | null;
  entry: CatalogEntry;
  onCancel: () => void;
}) {
  const line =
    busy === null || busy.status === null
      ? "Sending it to your Mac…"
      : jobStatusLine({
          jobId: busy.jobId ?? "",
          status: busy.status,
          progress: busy.progress,
          queueDepth: busy.queueDepth,
        });

  return (
    <View style={{ gap: space[3] }}>
      <Progress fraction={busy?.progress ?? null} text={line} />
      <Caption>{line}</Caption>
      {busy?.cancellable === true ? (
        <Button
          label="Stop it"
          variant="ghost"
          onPress={onCancel}
          hint="Stops after the chunk your Mac is working on"
          style={{ alignSelf: "flex-start" }}
        />
      ) : (
        <Caption>
          {runsAsJob(entry)
            ? "This one cannot be interrupted. You can leave this screen — it keeps going on your Mac."
            : "This one cannot be interrupted. It should be quick."}
        </Caption>
      )}
    </View>
  );
}

function GateNotice({
  feature,
  fix,
  excluded,
}: {
  feature: string;
  fix: string;
  excluded: boolean;
}) {
  return (
    <Note tone="warn" title={feature}>
      <View style={{ gap: space[2] }}>
        <Body tone="dim">
          {excluded
            ? "The packaged Mac app leaves this one out on purpose. It needs a source install."
            : "Your Mac does not have this installed, so the tool cannot run."}
        </Body>
        {fix !== "" && (
          <View
            style={{
              backgroundColor: color.bg0,
              borderRadius: radius.sm,
              borderWidth: 1,
              borderColor: color.line,
              padding: space[3],
              gap: space[1],
            }}
          >
            <Type variant="kicker" tone="dim" style={{ textTransform: "uppercase" }}>
              Run this on the Mac
            </Type>
            <Mono selectable>{fix}</Mono>
          </View>
        )}
      </View>
    </Note>
  );
}

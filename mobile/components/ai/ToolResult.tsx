/**
 * What a read-only tool has to say.
 *
 * `lib/aiResults.ts` decides what the rows ARE; this file decides only what a
 * row looks like and what its button does. There is exactly one such button on
 * the phone — "Use this hook" — because it is the only result action that can
 * be completed here: a hook line is a sentence, and turning it into an overlay
 * is one dispatch. A timestamp, by contrast, needs the timeline to be useful,
 * so a moment is presented as something to read and go and find, not as a
 * button that would pretend to take you somewhere.
 *
 * The raw payload is always available under a disclosure. A reader can miss a
 * field a newer Mac started sending, and a user who can see the JSON can still
 * get their answer; a user who cannot is stuck.
 */

import { useState } from "react";
import { View } from "react-native";

import { Body, Button, Caption, Divider, Heading, Mono, Row, Type } from "../ui";
import { color, radius, space, tint } from "../../constants/theme";
import { timecode } from "../../lib/format";
import { resultView, type ResultRow } from "../../lib/aiResults";

function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v ?? null, null, 2);
  } catch {
    // A result containing a cycle is not something the Mac produces, but a
    // crash in a disclosure panel would take the whole screen with it.
    return String(v);
  }
}

const ISSUE_TONE = { error: "danger", warn: "warn", info: "info" } as const;

function RangeRow({ row }: { row: ResultRow & { kind: "range" } }) {
  const ranged = row.end > row.start;
  const stamp = ranged ? `${timecode(row.start)}–${timecode(row.end)}` : timecode(row.start);
  return (
    <View accessible accessibilityLabel={`${stamp}. ${row.text}`} style={{ gap: 2 }}>
      <Row>
        <Type variant="timecode" tone="info" style={{ fontVariant: ["tabular-nums"] }}>
          {stamp}
        </Type>
        {row.score !== undefined && <Mono>{row.score.toFixed(2)}</Mono>}
      </Row>
      {row.text !== "" && <Body tone="dim">{row.text}</Body>}
    </View>
  );
}

function IssueRow({ row }: { row: ResultRow & { kind: "issue" } }) {
  const skin = tint[ISSUE_TONE[row.level]];
  return (
    <View style={{ flexDirection: "row", gap: space[3], alignItems: "flex-start" }}>
      <View
        style={{
          marginTop: 5,
          width: 8,
          height: 8,
          borderRadius: radius.pill,
          backgroundColor: skin.fg,
        }}
      />
      <View style={{ flex: 1 }}>
        <Body tone="dim">{row.text}</Body>
      </View>
    </View>
  );
}

export interface ToolResultProps {
  tool: string;
  label: string;
  result: unknown;
  /** Only offered for hook candidates, and only while a run is possible. */
  onUseHook?: (text: string) => void;
  busy: boolean;
}

export function ToolResult({ tool, label, result, onUseHook, busy }: ToolResultProps) {
  const view = resultView(tool, result, label);
  const [rawOpen, setRawOpen] = useState(false);

  return (
    <View style={{ gap: space[3] }}>
      <Heading>{view.headline}</Heading>
      {view.note !== undefined && <Caption>{view.note}</Caption>}

      {view.rows.length > 0 && (
        <View style={{ gap: space[3] }}>
          {view.rows.map((row, i) => (
            <View key={i} style={{ gap: space[3] }}>
              {i > 0 && <Divider />}
              {row.kind === "range" && <RangeRow row={row} />}
              {row.kind === "issue" && <IssueRow row={row} />}
              {row.kind === "text" && (
                <View style={{ gap: space[2] }}>
                  <Body>{row.text}</Body>
                  {onUseHook !== undefined && (
                    <Button
                      label="Use this hook"
                      variant="neutral"
                      disabled={busy}
                      hint="Adds it to the project as a hook overlay"
                      onPress={() => onUseHook(row.text)}
                      style={{ alignSelf: "flex-start" }}
                    />
                  )}
                </View>
              )}
            </View>
          ))}
        </View>
      )}

      <View style={{ gap: space[2] }}>
        <Button
          label={rawOpen ? "Hide raw result" : "Show raw result"}
          variant="ghost"
          onPress={() => setRawOpen((o) => !o)}
          style={{ alignSelf: "flex-start" }}
        />
        {rawOpen && (
          <View
            style={{
              backgroundColor: color.bg0,
              borderRadius: radius.sm,
              borderWidth: 1,
              borderColor: color.line,
              padding: space[3],
            }}
          >
            <Mono selectable>{safeJson(view.raw)}</Mono>
          </View>
        )}
      </View>
    </View>
  );
}

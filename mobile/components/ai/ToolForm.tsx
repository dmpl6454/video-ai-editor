/**
 * The generated form for one tool.
 *
 * Every control here is built from a `Field` that `lib/schemaForm.ts` derived
 * from the Mac's own `input_schema`, so the phone never carries a hand-written
 * form that can drift from the handler it calls. What this file decides is
 * only how each widget LOOKS and behaves under a thumb:
 *
 *   - a select is a row of chips, not a dropdown. There is no picker package
 *     in this app, and a wheel that covers half the screen to choose between
 *     "9:16" and "16:9" is worse than four things you can see at once;
 *   - a bounded integer is a stepper, because two taps beat summoning a
 *     numeric keyboard and dismissing it again;
 *   - a nullable argument gets its own switch above the input. `null` means
 *     something specific for these — true alpha, no loudness pass — and it is
 *     NOT the same as leaving the field blank, which omits the argument and
 *     leaves the Mac on its default.
 *
 * The clip picker is the one control with no desktop counterpart. The Mac
 * takes `clip_id` from the timeline selection and hides the field; the phone's
 * shared store has no selection at all, so the field is shown and filled from
 * the EDL. That is the honest control, and it is also the better one — you can
 * see which clip you are about to spend twenty minutes upscaling.
 */

import { Switch, TextInput, View } from "react-native";

import { Body, Caption, Chip, Field as TextField, Row, Type } from "../ui";
import { color, HIT_SLOP_MIN, radius, space, type } from "../../constants/theme";
import { clampToField, type Field } from "../../lib/schemaForm";
import { basename, timecode } from "../../lib/format";
import { isMediaClip, type EDL } from "../../lib/types";

export interface ClipChoice {
  id: string;
  label: string;
  sub: string;
}

/** Chips wrap between themselves but do not shrink below their own text, so a
 *  clip called `interview_take_3_final_v2.mp4` would push the row off screen.
 *  The middle is what differs between takes, so the tail is what survives. */
const MAX_CHIP_LABEL = 22;

function shortName(path: string): string {
  const name = basename(path);
  return name.length <= MAX_CHIP_LABEL ? name : `…${name.slice(name.length - MAX_CHIP_LABEL + 1)}`;
}

/**
 * The clips a picker offers. `"video"` checks the TRACK type rather than just
 * `isMediaClip`, because a music clip is also a media clip and upscaling one
 * is a 422 — the same trap the desktop catalogue documents.
 */
export function clipChoices(edl: EDL | null, filter: "video" | "media"): ClipChoice[] {
  if (edl === null) return [];
  const out: ClipChoice[] = [];
  for (const track of edl.tracks) {
    if (filter === "video" && track.type !== "video") continue;
    for (const clip of track.clips) {
      if (!isMediaClip(clip)) continue;
      out.push({
        id: clip.id,
        label: shortName(clip.src),
        sub: `${track.label ?? track.id} · ${timecode(clip.start)}`,
      });
    }
  }
  return out;
}

interface FieldProps {
  field: Field;
  value: unknown;
  error: string | null;
  disabled: boolean;
  clips: ClipChoice[];
  onChange: (name: string, value: unknown) => void;
}

const asText = (v: unknown): string => (v === null || v === undefined ? "" : String(v));

function helpFor(f: Field): string | undefined {
  // The catalogue's `help` is written for this screen; the schema's
  // `description` was written for Claude. Prefer the former, fall back rather
  // than show nothing — an unexplained `keep_pad` is worse than a terse one.
  return f.help ?? f.description;
}

function SelectField({ field, value, disabled, onChange }: FieldProps) {
  const options = field.options ?? [];
  return (
    <View style={{ gap: space[2] }}>
      <Type variant="label" tone="dim">
        {field.label}
      </Type>
      <Row accessibilityRole="radiogroup" accessibilityLabel={field.label}>
        {!field.required && (
          <Chip
            label="Default"
            kind="choice"
            selected={value === "" || value === undefined}
            disabled={disabled}
            onPress={() => onChange(field.name, "")}
          />
        )}
        {options.map((o) => (
          <Chip
            key={String(o)}
            label={String(o)}
            kind="choice"
            selected={String(value) === String(o)}
            disabled={disabled}
            onPress={() => onChange(field.name, o)}
          />
        ))}
      </Row>
      {helpFor(field) !== undefined && <Caption>{helpFor(field)}</Caption>}
    </View>
  );
}

function SwitchField({ field, value, disabled, onChange }: FieldProps) {
  const on = value === true;
  return (
    <View style={{ gap: space[1] }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: space[3], minHeight: HIT_SLOP_MIN }}>
        <View style={{ flex: 1 }}>
          <Type variant="label">{field.label}</Type>
        </View>
        <Switch
          value={on}
          disabled={disabled}
          onValueChange={(next) => onChange(field.name, next)}
          accessibilityLabel={field.label}
          trackColor={{ false: color.bg3, true: color.accent.secondary }}
          thumbColor={color.text}
          ios_backgroundColor={color.bg3}
        />
      </View>
      {helpFor(field) !== undefined && <Caption>{helpFor(field)}</Caption>}
    </View>
  );
}

function StepperField({ field, value, error, disabled, onChange }: FieldProps) {
  const current = typeof value === "number" ? value : Number(asText(value));
  const shown = Number.isFinite(current) ? current : (field.min ?? 0);
  // A stepper only exists for a bounded range (schemaForm::numericWidget), so
  // a fractional one gets ten stops across that range rather than 1.0 jumps
  // that would cross a 0..1 argument in a single tap. Rounding keeps 0.1 + 0.2
  // from showing up as 0.30000000000000004 in the control.
  const span = field.min !== undefined && field.max !== undefined ? field.max - field.min : 10;
  const step = field.integer === true ? 1 : Math.max(Math.round((span / 10) * 100) / 100, 0.01);
  const nudge = (by: number) =>
    onChange(field.name, Math.round(clampToField(field, shown + by) * 1000) / 1000);
  const atMin = field.min !== undefined && shown <= field.min;
  const atMax = field.max !== undefined && shown >= field.max;
  return (
    <View style={{ gap: space[1] }}>
      <Type variant="label" tone="dim">
        {field.label}
      </Type>
      <View
        accessible
        accessibilityRole="adjustable"
        accessibilityLabel={field.label}
        accessibilityValue={{ min: field.min, max: field.max, now: shown, text: String(shown) }}
        accessibilityActions={[{ name: "increment" }, { name: "decrement" }]}
        onAccessibilityAction={(e) => nudge(e.nativeEvent.actionName === "increment" ? step : -step)}
        style={{
          flexDirection: "row",
          alignItems: "center",
          alignSelf: "flex-start",
          borderRadius: radius.sm,
          borderWidth: 1,
          borderColor: error === null ? color.lineStrong : color.accent.warn,
          backgroundColor: color.bg2,
          overflow: "hidden",
        }}
      >
        <StepButton label="−" disabled={disabled || atMin} onPress={() => nudge(-step)} />
        <Type
          variant="timecode"
          style={{ minWidth: 56, textAlign: "center", fontVariant: ["tabular-nums"] }}
        >
          {String(shown)}
        </Type>
        <StepButton label="+" disabled={disabled || atMax} onPress={() => nudge(step)} />
      </View>
      {error !== null ? (
        <Type variant="caption" tone="warn" accessibilityLiveRegion="polite">
          {error}
        </Type>
      ) : (
        helpFor(field) !== undefined && <Caption>{helpFor(field)}</Caption>
      )}
    </View>
  );
}

function StepButton({
  label,
  disabled,
  onPress,
}: {
  label: string;
  disabled: boolean;
  onPress: () => void;
}) {
  return (
    <Chip label={label} kind="toggle" selected={false} disabled={disabled} onPress={onPress} />
  );
}

function ParagraphField({ field, value, error, disabled, onChange }: FieldProps) {
  return (
    <View style={{ gap: space[1] }}>
      <Type variant="label" tone="dim">
        {field.label}
      </Type>
      <TextInput
        value={asText(value)}
        onChangeText={(t) => onChange(field.name, t)}
        editable={!disabled}
        multiline
        accessibilityLabel={field.label}
        placeholderTextColor={color.textDim}
        maxFontSizeMultiplier={type.body.maxScale}
        style={{
          minHeight: 88,
          textAlignVertical: "top",
          borderRadius: radius.sm,
          borderWidth: 1,
          borderColor: error === null ? color.line : color.accent.warn,
          backgroundColor: color.bg2,
          paddingHorizontal: space[3],
          paddingVertical: space[3],
          color: color.text,
          fontFamily: type.body.family,
          fontSize: type.body.size,
          lineHeight: type.body.lineHeight,
        }}
      />
      {error !== null ? (
        <Type variant="caption" tone="warn" accessibilityLiveRegion="polite">
          {error}
        </Type>
      ) : (
        helpFor(field) !== undefined && <Caption>{helpFor(field)}</Caption>
      )}
    </View>
  );
}

function ClipField({ field, value, error, disabled, clips, onChange }: FieldProps) {
  if (clips.length === 0) {
    return (
      <View style={{ gap: space[1] }}>
        <Type variant="label" tone="dim">
          {field.label}
        </Type>
        <Body tone="dim">
          This project has no {field.clipFilter === "video" ? "video" : "media"} clip to run it on
          yet.
        </Body>
      </View>
    );
  }
  return (
    <View style={{ gap: space[2] }}>
      <Type variant="label" tone="dim">
        {field.label}
      </Type>
      <Row accessibilityRole="radiogroup" accessibilityLabel={field.label}>
        {clips.map((c) => (
          <Chip
            key={c.id}
            label={c.label}
            sub={c.sub}
            kind="choice"
            selected={value === c.id}
            disabled={disabled}
            onPress={() => onChange(field.name, c.id)}
          />
        ))}
      </Row>
      {error !== null && (
        <Type variant="caption" tone="warn" accessibilityLiveRegion="polite">
          {error}
        </Type>
      )}
    </View>
  );
}

function TimeHint({ value }: { value: unknown }) {
  const n = typeof value === "number" ? value : Number(asText(value));
  if (!Number.isFinite(n)) return null;
  return <Caption>{`${timecode(n)} into the project`}</Caption>;
}

function OneField(props: FieldProps) {
  const { field, value, error, disabled, onChange } = props;

  // A nullable field is two controls: the switch that chooses "send an
  // explicit null" and, when it is off, the value control underneath.
  const nulled = field.nullable !== undefined && value === null;
  const inner = () => {
    switch (field.widget) {
      case "select":
        return <SelectField {...props} />;
      case "switch":
        return <SwitchField {...props} />;
      case "stepper":
        return <StepperField {...props} />;
      case "paragraph":
      case "mapping":
        return <ParagraphField {...props} />;
      case "clip":
        return <ClipField {...props} />;
      case "number":
      case "time":
        return (
          <View style={{ gap: space[1] }}>
            <TextField
              label={field.label}
              value={asText(value)}
              onChangeText={(t) => onChange(field.name, t)}
              keyboardType="numeric"
              error={error}
              {...(helpFor(field) !== undefined ? { hint: helpFor(field) } : {})}
            />
            {field.widget === "time" && error === null && <TimeHint value={value} />}
          </View>
        );
      default: {
        // `list` and `text` share the control; only the hint differs, and a
        // list field with no hint is one the user fills in with spaces and
        // then wonders why it became a single item.
        const hint =
          field.widget === "list"
            ? `${helpFor(field) ?? "One or more"} — separate them with commas.`
            : helpFor(field);
        return (
          <TextField
            label={field.label}
            value={asText(value)}
            onChangeText={(t) => onChange(field.name, t)}
            autoCapitalize="sentences"
            error={error}
            {...(hint !== undefined ? { hint } : {})}
          />
        );
      }
    }
  };

  if (field.nullable === undefined) return inner();
  return (
    <View style={{ gap: space[2] }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: space[3], minHeight: HIT_SLOP_MIN }}>
        <View style={{ flex: 1 }}>
          <Type variant="label">{field.nullable.label}</Type>
        </View>
        <Switch
          value={nulled}
          disabled={disabled}
          onValueChange={(on) => onChange(field.name, on ? null : (field.default ?? ""))}
          accessibilityLabel={field.nullable.label}
          trackColor={{ false: color.bg3, true: color.accent.secondary }}
          thumbColor={color.text}
          ios_backgroundColor={color.bg3}
        />
      </View>
      {!nulled && inner()}
    </View>
  );
}

export interface ToolFormProps {
  fields: readonly Field[];
  values: Record<string, unknown>;
  errors: Record<string, string>;
  disabled: boolean;
  edl: EDL | null;
  onChange: (name: string, value: unknown) => void;
}

export function ToolForm({ fields, values, errors, disabled, edl, onChange }: ToolFormProps) {
  if (fields.length === 0) return null;
  return (
    <View style={{ gap: space[4] }}>
      {fields.map((f) => (
        <OneField
          key={f.name}
          field={f}
          value={values[f.name]}
          error={errors[f.name] ?? null}
          disabled={disabled}
          clips={f.widget === "clip" ? clipChoices(edl, f.clipFilter ?? "media") : []}
          onChange={onChange}
        />
      ))}
    </View>
  );
}

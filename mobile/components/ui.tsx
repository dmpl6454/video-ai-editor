/**
 * The component kit.
 *
 * ONE THEME, ON PURPOSE. This is a companion to a colour-grading tool: the
 * desktop editor is dark because a bright chrome around a video frame changes
 * how you judge the frame, and that reason does not stop being true on a
 * phone. So there is no light palette and no `useColorScheme()` here — a
 * half-hearted light mode would be a worse product than a committed dark one.
 * `app.json` pins `userInterfaceStyle: "dark"` to match.
 *
 * WHAT MAKES IT NOT A TEMPLATE. Four things carry the design, and every screen
 * in the app is built from them:
 *   - depth by layering, not by shadow: bg0 page → bg1 card with a hairline →
 *     bg2 inset row, plus a single lighter pixel along a raised card's top
 *     edge so it reads as lit from above the way physical hardware does;
 *   - a mono eyebrow over a tightly-tracked display line, so hierarchy comes
 *     from contrast of FORM as well as size;
 *   - tinted notes with a 3px rule rather than filled alert boxes, so guidance
 *     sits beside the content instead of shouting over it;
 *   - pressed states that move: every control scales to 0.98 and lifts its
 *     surface, on `transform`/`opacity` only, and not at all when the user has
 *     asked for reduced motion.
 *
 * Every text style takes its `maxFontSizeMultiplier` from the token that
 * defines it, so Dynamic Type is handled once here rather than forgotten in
 * thirty call sites.
 */

import { useEffect, useRef } from "react";
import {
  AccessibilityInfo,
  ActivityIndicator,
  FlatList,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type ListRenderItem,
  type PressableProps,
  type RefreshControlProps,
  type ViewToken,
  type StyleProp,
  type TextProps,
  type TextStyle,
  type ViewProps,
  type ViewStyle,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import {
  color,
  fill,
  HIT_SLOP_MIN,
  radius,
  space,
  tint,
  type,
  useReducedMotion,
  type FillName,
  type TintName,
  type TypeName,
} from "../constants/theme";

// ---------------------------------------------------------------------------
// Accessibility
// ---------------------------------------------------------------------------

/** Speak a message through VoiceOver whenever it changes — job progress, an
 *  error, a connection state. Silent when the message is unchanged. */
export function useAnnounce(message: string | null | undefined): void {
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (!message || message === last.current) return;
    last.current = message;
    AccessibilityInfo.announceForAccessibility(message);
  }, [message]);
}

// ---------------------------------------------------------------------------
// Type
// ---------------------------------------------------------------------------

export interface TypographyProps extends TextProps {
  variant?: TypeName;
  /** `dim` is the only secondary tone; there is no third, because a third
   *  would not clear 4.5:1 on these surfaces. */
  tone?: "default" | "dim" | TintName;
  children: React.ReactNode;
}

function toneColor(tone: TypographyProps["tone"]): string {
  if (tone === undefined || tone === "default") return color.text;
  if (tone === "dim") return color.textDim;
  return tint[tone].fg;
}

/** The single text primitive. Everything below is a preset of it. */
export function Type({ variant = "body", tone, style, children, ...rest }: TypographyProps) {
  const t = type[variant];
  const base: TextStyle = {
    fontFamily: t.family,
    fontSize: t.size,
    lineHeight: t.lineHeight,
    color: toneColor(tone),
  };
  if ("letterSpacing" in t) base.letterSpacing = t.letterSpacing;
  return (
    <Text {...rest} maxFontSizeMultiplier={t.maxScale} style={[base, style]}>
      {children}
    </Text>
  );
}

export function Display(p: Omit<TypographyProps, "variant">) {
  return <Type accessibilityRole="header" variant="display" {...p} />;
}
export function Title(p: Omit<TypographyProps, "variant">) {
  return <Type accessibilityRole="header" variant="title" {...p} />;
}
export function Heading(p: Omit<TypographyProps, "variant">) {
  return <Type accessibilityRole="header" variant="heading" {...p} />;
}
export function Body(p: Omit<TypographyProps, "variant">) {
  return <Type variant="body" {...p} />;
}
export function Caption(p: Omit<TypographyProps, "variant">) {
  return <Type variant="caption" tone="dim" {...p} />;
}
export function Mono(p: Omit<TypographyProps, "variant">) {
  return <Type variant="mono" tone="dim" {...p} />;
}

/** Tabular by construction — a timecode that shimmers as digits change is
 *  the fastest way to make a scrub feel broken. */
export function Timecode({ style, ...p }: Omit<TypographyProps, "variant">) {
  return <Type variant="timecode" style={[{ fontVariant: ["tabular-nums"] }, style]} {...p} />;
}

/** The uppercase mono eyebrow. Carries the accent colour, never the fill. */
export function Kicker({ children, style, ...rest }: Omit<TypographyProps, "variant" | "tone">) {
  return (
    <Type
      variant="kicker"
      tone="info"
      style={[{ textTransform: "uppercase" }, style]}
      {...rest}
    >
      {children}
    </Type>
  );
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export interface ScreenProps {
  kicker?: string;
  title: string;
  lede?: string;
  /** Optional: a screen that is still loading has a title and nothing else. */
  children?: React.ReactNode;
  refreshControl?: React.ReactElement<RefreshControlProps>;
  /** "none" when a native header already clears the status bar. */
  inset?: "safe" | "none";
  /** Pinned above the scroll — the connection strip lives here. */
  header?: React.ReactNode;
  /** Pinned below — a primary action that must never scroll away. */
  footer?: React.ReactNode;
}

/** A page with the app's rhythm: eyebrow, title, lede, then content. */
export function Screen({ kicker, title, lede, children, refreshControl, inset = "safe", header, footer }: ScreenProps) {
  const insets = useSafeAreaInsets();
  return (
    <View style={{ flex: 1, backgroundColor: color.bg0 }}>
      {header && <View style={{ paddingTop: inset === "safe" ? insets.top : 0 }}>{header}</View>}
      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={{
          paddingHorizontal: space[5],
          paddingTop: header ? space[4] : (inset === "safe" ? insets.top : 0) + space[5],
          paddingBottom: space[8],
          gap: space[4],
        }}
        keyboardDismissMode="on-drag"
        refreshControl={refreshControl}
      >
        <View style={{ gap: space[2], marginBottom: space[1] }}>
          {kicker !== undefined && <Kicker>{kicker}</Kicker>}
          <Display>{title}</Display>
          {lede !== undefined && <Body tone="dim">{lede}</Body>}
        </View>
        {children}
      </ScrollView>
      {footer && (
        <View
          style={{
            paddingHorizontal: space[5],
            paddingTop: space[3],
            paddingBottom: insets.bottom + space[3],
            borderTopWidth: StyleSheet.hairlineWidth,
            borderTopColor: color.line,
            backgroundColor: color.bg1,
            gap: space[2],
          }}
        >
          {footer}
        </View>
      )}
    </View>
  );
}

export interface ScreenListProps<T> extends Omit<ScreenProps, "children"> {
  data: readonly T[];
  keyExtractor: (item: T) => string;
  renderItem: ListRenderItem<T>;
  /** Rendered above the list, inside the scroll — cards, notes, the intro. */
  above?: React.ReactNode;
  /** Rendered below the list, inside the scroll. */
  below?: React.ReactNode;
  /** Which rows are on screen right now. Screens use this to fetch only what
   *  the user can actually see. */
  onVisibleChange?: (ids: readonly string[]) => void;
}

/**
 * `Screen`, but the content is a VIRTUALIZED list.
 *
 * Exists because `app/projects.tsx` rendered `sessions.map(...)` inside
 * `Screen`'s ScrollView: every row and every row's `expo-image` poster mounted
 * at once, and `GET /api/sessions` is unpaginated. On a Mac with 200 projects
 * that is 200 mounted rows behind a screen showing six, plus several seconds of
 * jank at mount. `onVisibleChange` is the other half — it lets a screen fetch
 * per-row extras for the rows in view instead of walking the whole list, which
 * also keeps every signed poster URL well inside the media token's 60 s life.
 *
 * Deliberately a sibling of `Screen` rather than a mode of it: the two have
 * genuinely different children (nodes vs. data + renderer), and a `Screen` that
 * took both would be a worse version of each.
 */
export function ScreenList<T>({
  kicker,
  title,
  lede,
  data,
  keyExtractor,
  renderItem,
  above,
  below,
  refreshControl,
  inset = "safe",
  header,
  footer,
  onVisibleChange,
}: ScreenListProps<T>) {
  const insets = useSafeAreaInsets();

  // FlatList requires this to be stable for the life of the list — a new object
  // on any render throws "Changing onViewableItemsChanged on the fly".
  const viewabilityConfig = useRef({ itemVisiblePercentThreshold: 10 }).current;
  const visibleRef = useRef(onVisibleChange);
  visibleRef.current = onVisibleChange;
  const onViewableItemsChanged = useRef(({ viewableItems }: { viewableItems: ViewToken[] }) => {
    visibleRef.current?.(
      viewableItems.map((v) => v.key).filter((k): k is string => typeof k === "string"),
    );
  }).current;

  return (
    <View style={{ flex: 1, backgroundColor: color.bg0 }}>
      {header && <View style={{ paddingTop: inset === "safe" ? insets.top : 0 }}>{header}</View>}
      <FlatList
        style={{ flex: 1 }}
        data={data as T[]}
        keyExtractor={keyExtractor}
        renderItem={renderItem}
        contentContainerStyle={{
          paddingHorizontal: space[5],
          paddingTop: header ? space[4] : (inset === "safe" ? insets.top : 0) + space[5],
          paddingBottom: space[8],
          gap: space[4],
        }}
        keyboardDismissMode="on-drag"
        refreshControl={refreshControl}
        onViewableItemsChanged={onViewableItemsChanged}
        viewabilityConfig={viewabilityConfig}
        ListHeaderComponent={
          <View style={{ gap: space[4], marginBottom: space[4] }}>
            <View style={{ gap: space[2], marginBottom: space[1] }}>
              {kicker !== undefined && <Kicker>{kicker}</Kicker>}
              <Display>{title}</Display>
              {lede !== undefined && <Body tone="dim">{lede}</Body>}
            </View>
            {above}
          </View>
        }
        ListFooterComponent={below === undefined ? null : <View style={{ marginTop: space[4] }}>{below}</View>}
      />
      {footer && (
        <View
          style={{
            paddingHorizontal: space[5],
            paddingTop: space[3],
            paddingBottom: insets.bottom + space[3],
            borderTopWidth: StyleSheet.hairlineWidth,
            borderTopColor: color.line,
            backgroundColor: color.bg1,
            gap: space[2],
          }}
        >
          {footer}
        </View>
      )}
    </View>
  );
}

export interface CardProps extends ViewProps {
  /** "raised" gets the lit top edge; "inset" is a recessed row inside one. */
  tone?: "raised" | "inset";
}

export function Card({ tone = "raised", style, children, ...rest }: CardProps) {
  const raised = tone === "raised";
  return (
    <View
      {...rest}
      style={[
        {
          backgroundColor: raised ? color.bg1 : color.bg2,
          borderRadius: radius.md,
          borderWidth: StyleSheet.hairlineWidth,
          borderColor: color.line,
          padding: space[4],
          gap: space[3],
        },
        // One lighter pixel along the top edge. This is the whole "lit from
        // above" effect; a drop shadow on a near-black ground renders as mud.
        raised && { borderTopColor: color.lineStrong, borderTopWidth: 1 },
        style,
      ]}
    >
      {children}
    </View>
  );
}

export function Row({ style, children, ...rest }: ViewProps) {
  return (
    <View {...rest} style={[styles.row, style]}>
      {children}
    </View>
  );
}

export function Divider() {
  return <View style={{ height: StyleSheet.hairlineWidth, backgroundColor: color.line }} />;
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

export interface ButtonProps extends Omit<PressableProps, "style" | "children"> {
  label: string;
  variant?: FillName | "ghost";
  busy?: boolean;
  /** Right-aligned supporting text — a size, a duration, a shortcut. */
  hint?: string;
  style?: StyleProp<ViewStyle>;
}

export function Button({ label, variant = "primary", busy, hint, disabled, style, ...rest }: ButtonProps) {
  const reduced = useReducedMotion();
  const ghost = variant === "ghost";
  const skin = ghost ? null : fill[variant];
  const fg = skin ? skin.fg : color.text;
  const isDisabled = disabled === true || busy === true;

  return (
    <Pressable
      {...rest}
      disabled={isDisabled}
      accessibilityRole="button"
      accessibilityState={{ disabled: isDisabled, busy: busy === true }}
      accessibilityHint={hint}
      style={({ pressed }) => [
        {
          minHeight: HIT_SLOP_MIN,
          paddingVertical: 12,
          paddingHorizontal: space[4],
          borderRadius: radius.sm,
          backgroundColor: skin ? skin.bg : pressed ? color.bg3 : color.bg2,
          borderWidth: 1,
          borderColor: skin ? skin.bg : color.lineStrong,
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: space[2],
          opacity: isDisabled ? 0.45 : pressed ? 0.92 : 1,
          // transform + opacity only: both run on the compositor, so a press
          // stays responsive while the timeline is decoding thumbnails.
          transform: pressed && !reduced ? [{ scale: 0.98 }] : [{ scale: 1 }],
        },
        style,
      ]}
    >
      {busy === true && <ActivityIndicator color={fg} size="small" />}
      <Text
        maxFontSizeMultiplier={type.button.maxScale}
        style={{
          fontFamily: type.button.family,
          fontSize: type.button.size,
          lineHeight: type.button.lineHeight,
          color: fg,
          flexShrink: 1,
          textAlign: "center",
        }}
      >
        {label}
      </Text>
    </Pressable>
  );
}

export interface ChipProps {
  label: string;
  sub?: string;
  selected?: boolean;
  onPress?: () => void;
  disabled?: boolean;
  /** "toggle" is an independent on/off (a checkbox to VoiceOver); "choice" is
   *  one of a mutually exclusive set — wrap the set in a radiogroup. */
  kind?: "toggle" | "choice";
}

export function Chip({ label, sub, selected = false, onPress, disabled = false, kind = "toggle" }: ChipProps) {
  const reduced = useReducedMotion();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole={kind === "choice" ? "radio" : "checkbox"}
      accessibilityState={{ checked: selected, disabled }}
      accessibilityLabel={sub === undefined ? label : `${label}, ${sub}`}
      style={({ pressed }) => ({
        minHeight: HIT_SLOP_MIN,
        justifyContent: "center",
        paddingVertical: space[2],
        paddingHorizontal: space[3],
        borderRadius: radius.sm,
        borderWidth: 1,
        borderColor: selected ? color.accent.secondary : color.lineStrong,
        backgroundColor: selected ? color.accent.secondarySoft : pressed ? color.bg3 : color.bg2,
        opacity: disabled ? 0.45 : 1,
        transform: pressed && !reduced ? [{ scale: 0.98 }] : [{ scale: 1 }],
      })}
    >
      <Type variant="label" tone={selected ? "info" : "default"}>
        {label}
      </Type>
      {sub !== undefined && <Mono>{sub}</Mono>}
    </Pressable>
  );
}

export interface FieldProps {
  label: string;
  value: string;
  onChangeText: (v: string) => void;
  placeholder?: string;
  /** Shown under the input in the warn tone. Its presence marks the field
   *  invalid to assistive tech, so it must be null when the value is good. */
  error?: string | null;
  hint?: string;
  keyboardType?: "default" | "numeric" | "url";
  autoCapitalize?: "none" | "sentences";
  autoCorrect?: boolean;
  onSubmitEditing?: () => void;
  returnKeyType?: "done" | "next" | "go";
}

export function Field({
  label,
  value,
  onChangeText,
  placeholder,
  error = null,
  hint,
  keyboardType = "default",
  autoCapitalize = "none",
  autoCorrect = false,
  onSubmitEditing,
  returnKeyType = "done",
}: FieldProps) {
  return (
    <View style={{ gap: space[1] }}>
      <Type variant="label" tone="dim">
        {label}
      </Type>
      <TextInput
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={color.textDim}
        keyboardType={keyboardType}
        autoCapitalize={autoCapitalize}
        autoCorrect={autoCorrect}
        onSubmitEditing={onSubmitEditing}
        returnKeyType={returnKeyType}
        accessibilityLabel={label}
        accessibilityHint={hint}
        maxFontSizeMultiplier={type.body.maxScale}
        style={{
          minHeight: HIT_SLOP_MIN,
          borderRadius: radius.sm,
          borderWidth: 1,
          borderColor: error === null ? color.line : color.accent.warn,
          backgroundColor: color.bg2,
          paddingHorizontal: space[3],
          paddingVertical: space[2],
          color: color.text,
          fontFamily: type.body.family,
          fontSize: type.body.size,
        }}
      />
      {error !== null && (
        <Type variant="caption" tone="warn" accessibilityLiveRegion="polite">
          {error}
        </Type>
      )}
      {error === null && hint !== undefined && <Caption>{hint}</Caption>}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

export interface NoteProps {
  tone?: TintName;
  title?: string;
  children: React.ReactNode;
}

/** Guidance beside the content, not an alert box over it. */
export function Note({ tone = "info", title, children }: NoteProps) {
  const skin = tint[tone];
  return (
    <View
      accessibilityLiveRegion={tone === "danger" ? "assertive" : "polite"}
      accessibilityRole={tone === "danger" ? "alert" : undefined}
      style={{
        backgroundColor: skin.bg,
        borderLeftWidth: 3,
        borderLeftColor: skin.fg,
        borderRadius: radius.sm,
        paddingVertical: space[3],
        paddingHorizontal: space[4],
        gap: space[1],
      }}
    >
      {title !== undefined && (
        <Type variant="label" tone={tone}>
          {title}
        </Type>
      )}
      {typeof children === "string" ? <Body tone="dim">{children}</Body> : children}
    </View>
  );
}

export function Badge({ tone = "info", label }: { tone?: TintName; label: string }) {
  const skin = tint[tone];
  return (
    <View style={{ paddingVertical: 3, paddingHorizontal: space[2], borderRadius: radius.pill, backgroundColor: skin.bg }}>
      <Type variant="caption" style={{ color: skin.fg }}>
        {label}
      </Type>
    </View>
  );
}

/** One measured fact. Read as a single unit by VoiceOver. */
export function Stat({ value, label, tone }: { value: string; label: string; tone?: TintName }) {
  return (
    <View
      accessible
      accessibilityLabel={`${label}: ${value}`}
      style={{
        minWidth: 104,
        flexGrow: 1,
        flexShrink: 1,
        padding: space[3],
        borderRadius: radius.sm,
        backgroundColor: tone === undefined ? color.bg2 : tint[tone].bg,
        gap: 2,
      }}
    >
      <Type
        variant="title"
        style={{ color: tone === undefined ? color.text : tint[tone].fg, fontVariant: ["tabular-nums"] }}
      >
        {value}
      </Type>
      <Type variant="kicker" tone="dim" style={{ textTransform: "uppercase" }}>
        {label}
      </Type>
    </View>
  );
}

export interface ProgressProps {
  /** 0..1, or null for indeterminate — which is what a handler that reports no
   *  progress, or an upload with no known total, must show. A confident 0%
   *  that never moves is read as a hang. */
  fraction: number | null;
  /** What a screen reader says for the current value. */
  text: string;
  tone?: "info" | "danger";
}

export function Progress({ fraction, text, tone = "info" }: ProgressProps) {
  const bar = tone === "danger" ? color.danger : color.accent.secondary;
  const pct = fraction === null ? null : Math.round(Math.min(1, Math.max(0, fraction)) * 100);
  return (
    <View
      accessibilityRole="progressbar"
      accessibilityValue={pct === null ? { text } : { min: 0, max: 100, now: pct, text }}
      style={{ height: 6, borderRadius: radius.pill, backgroundColor: color.bg3, overflow: "hidden" }}
    >
      {pct === null ? (
        // Indeterminate without animation: a half-width band at rest reads as
        // "working, amount unknown" and cannot lie about how far along it is.
        <View style={{ height: "100%", width: "40%", backgroundColor: bar, opacity: 0.55 }} />
      ) : (
        <View style={{ height: "100%", width: `${pct}%`, backgroundColor: bar }} />
      )}
    </View>
  );
}

/**
 * The honesty component. Every screen that cannot work without the Mac renders
 * this when the link is down. It exists as a component rather than a sentence
 * per screen so it cannot be quietly omitted from one of them.
 */
export function MacRequired({ message }: { message: string }) {
  return (
    <Note tone="warn" title="Needs your Mac">
      {message}
    </Note>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", flexWrap: "wrap", gap: space[2], alignItems: "center" },
});

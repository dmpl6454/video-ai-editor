import { useEffect, useState } from "react";
import { AccessibilityInfo } from "react-native";

/**
 * Design tokens for the iOS companion.
 *
 * WHY these exact hexes: the desktop editor's palette lives in
 * frontend/src/styles.css:4-21 and it is the product's identity — a phone that
 * showed a different pink would read as a different app. Every value below is
 * copied character-for-character from that block. Where the phone needs a
 * token the desktop never had (a `danger` distinct from the brand accent, the
 * soft tinted surfaces that notes sit on), it is marked NEW and its contrast
 * was computed before it was written down, not after.
 *
 * WHY the tokens are grouped rather than flat: `accent.primary` is a FILL and
 * nothing else. The desktop paints `button.primary { background: var(--accent);
 * color: #fff }` — white on #ff4d6d is 3.21:1, which fails WCAG AA for normal
 * text. The phone corrects that by putting near-black ink on the fill (6.0:1)
 * and by never using the brand pink as a text colour at all. `contrastPairs`
 * below is the machine-checkable statement of that rule; __tests__/lib/
 * theme.test.ts walks it.
 *
 * (The 0.6.0 spec asserted #FF4D6D on #0E0E10 was ~4.4:1. Measured, it is
 * 6.00:1 — the pair passes. The fill-only rule is kept anyway because it is a
 * better system, and because the pairing it actually rules out, white-on-pink
 * at 3.21:1, is a genuine failure the desktop still ships.)
 */

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

export const color = {
  /** Page ground. The deepest surface; a phone held in a dark grading room. */
  bg0: "#0e0e10",
  /** Panels and cards that sit on the page. */
  bg1: "#16161a",
  /** Inputs, list rows, and the inside of a card. */
  bg2: "#1d1d22",
  /** Pressed states and the raised chrome of a control. */
  bg3: "#25252c",

  /** Hairlines. `lineStrong` is for a border that has to be found by eye. */
  line: "#2c2c34",
  lineStrong: "#3a3a45", // NEW — the desktop only ever needed one hairline.

  /** The only two text colours. A third, fainter tone would not clear 4.5:1. */
  text: "#e6e6eb",
  textDim: "#9b9ba5",

  accent: {
    /** Brand pink. A FILL — see the header. Never assign it to a text style. */
    primary: "#ff4d6d",
    /** Ink that sits ON `primary` (6.0:1). NEW — the desktop uses white here. */
    primaryInk: "#0e0e10",
    /** NEW — tinted ground for a brand-toned note or a selected chip. */
    primarySoft: "#2b1420",

    /** Informational / video-track blue (desktop --accent-2, --track-v). */
    secondary: "#5b8dff",
    secondarySoft: "#151f38", // NEW
    secondaryInk: "#0e0e10", // NEW

    /** Success (desktop --good, --track-a). */
    good: "#4ade80",
    goodSoft: "#12291c", // NEW
    goodInk: "#0e0e10", // NEW

    /** Caution and field-level errors (desktop --warn, --track-text). */
    warn: "#fbbf24",
    warnSoft: "#2c2410", // NEW
    warnInk: "#0e0e10", // NEW
  },

  /**
   * NEW. The desktop has no danger token — it reuses the brand pink for both
   * "primary action" and "destructive", which on a phone would mean the Export
   * button and the Delete button were the same colour. This is a warmer
   * orange-red (hue 7 vs the brand's 349) and, by policy, appears as tinted
   * text on `dangerSoft` rather than as a saturated fill, so the two can never
   * be confused at a glance even by a red-weak eye.
   */
  danger: "#ff6b57",
  dangerSoft: "#2e1a16",
  dangerInk: "#0e0e10",

  /** Timeline track identities — desktop --track-*. E's timeline reads these. */
  track: {
    video: "#5b8dff",
    audio: "#4ade80",
    music: "#a78bfa",
    text: "#fbbf24",
    captions: "#f472b6",
  },

  /** Playhead / selection. Desktop --selection. */
  selection: "#ffffff",

  /** Scrim behind a sheet or a modal. */
  scrim: "rgba(6,6,8,0.72)",
} as const;

/**
 * The tinted note/badge families. `components/ui.tsx` reads THIS map rather
 * than picking colours per call site, which is what makes `contrastPairs`
 * below an honest description of what the app renders instead of a wish.
 */
export const tint = {
  info: { fg: color.accent.secondary, bg: color.accent.secondarySoft },
  good: { fg: color.accent.good, bg: color.accent.goodSoft },
  warn: { fg: color.accent.warn, bg: color.accent.warnSoft },
  danger: { fg: color.danger, bg: color.dangerSoft },
  brand: { fg: color.accent.secondary, bg: color.accent.primarySoft },
} as const;

export type TintName = keyof typeof tint;

/** Solid fills that carry a label, and the ink that goes on each. */
export const fill = {
  primary: { bg: color.accent.primary, fg: color.accent.primaryInk },
  danger: { bg: color.danger, fg: color.dangerInk },
  neutral: { bg: color.bg3, fg: color.text },
} as const;

export type FillName = keyof typeof fill;

/** Every surface a text colour is ever painted on. */
const SURFACES = [
  ["bg0", color.bg0],
  ["bg1", color.bg1],
  ["bg2", color.bg2],
  ["bg3", color.bg3],
] as const;

export interface ContrastPair {
  fg: string;
  bg: string;
  /** Human description, so a failing assertion names the screen it breaks. */
  use: string;
}

/**
 * Every foreground/background pairing the component kit can actually produce.
 * Derived from `tint`/`fill` rather than hand-listed, so a new tone cannot be
 * added without the contrast test seeing it.
 */
export const contrastPairs: readonly ContrastPair[] = [
  ...SURFACES.flatMap(([name, bg]) => [
    { fg: color.text, bg, use: `body text on ${name}` },
    { fg: color.textDim, bg, use: `secondary text on ${name}` },
  ]),
  ...(Object.keys(tint) as TintName[]).flatMap((name) => [
    { fg: tint[name].fg, bg: tint[name].bg, use: `${name} note label on its own tint` },
    { fg: color.text, bg: tint[name].bg, use: `note body on the ${name} tint` },
    { fg: color.textDim, bg: tint[name].bg, use: `note detail on the ${name} tint` },
  ]),
  ...(Object.keys(fill) as FillName[]).map((name) => ({
    fg: fill[name].fg,
    bg: fill[name].bg,
    use: `label on the ${name} fill`,
  })),
  // Tinted text also appears directly on the page ground: a field error under
  // an input, a "Connected" line in the status strip. Both grounds, both tones.
  { fg: color.accent.warn, bg: color.bg0, use: "field error under an input" },
  { fg: color.accent.warn, bg: color.bg1, use: "field error inside a card" },
  { fg: color.accent.good, bg: color.bg0, use: "connected status line" },
  { fg: color.accent.good, bg: color.bg1, use: "connected status line in a card" },
  { fg: color.accent.secondary, bg: color.bg0, use: "inline link / job status" },
  { fg: color.accent.secondary, bg: color.bg1, use: "inline link inside a card" },
  { fg: color.danger, bg: color.bg0, use: "failure text on the page" },
  { fg: color.danger, bg: color.bg1, use: "failure text inside a card" },
];

// ---------------------------------------------------------------------------
// Type
// ---------------------------------------------------------------------------

/**
 * Sora for display, Inter for prose, IBM Plex Mono for anything the user has
 * to read as a number. The desktop's --font-ui names 'Inter' explicitly, so
 * body copy is genuinely the same face on both ends; Sora gives the phone the
 * headline character a system stack cannot. Mono is not decoration — timecodes
 * and byte counts change every frame, and tabular digits stop them shimmering.
 */
export const fonts = {
  display: "Sora_700Bold",
  displayMedium: "Sora_600SemiBold",
  body: "Inter_400Regular",
  bodyMedium: "Inter_500Medium",
  bodySemibold: "Inter_600SemiBold",
  bodyBold: "Inter_700Bold",
  mono: "IBMPlexMono_400Regular",
  monoMedium: "IBMPlexMono_500Medium",
} as const;

export interface TypeStyle {
  family: string;
  size: number;
  lineHeight: number;
  letterSpacing?: number;
  /**
   * Cap for `maxFontSizeMultiplier`. Every entry carries one: Dynamic Type at
   * the largest accessibility sizes would otherwise push a 30pt screen title
   * to 90pt and clip it. The bar is "nothing clips at fontScale 1.35", and
   * denser roles (timecode inside a fixed-height ruler) get a tighter cap than
   * prose, which is allowed to grow the most because it reflows.
   */
  maxScale: number;
}

export const type = {
  /** The one big thing on a screen. */
  display: { family: fonts.display, size: 30, lineHeight: 35, letterSpacing: -0.6, maxScale: 1.5 },
  /** Section head inside a scroll. */
  title: { family: fonts.displayMedium, size: 21, lineHeight: 27, letterSpacing: -0.3, maxScale: 1.6 },
  /** Card head. */
  heading: { family: fonts.bodyBold, size: 16, lineHeight: 22, letterSpacing: -0.1, maxScale: 1.7 },
  /** Prose. Reflows, so it may grow the furthest. */
  body: { family: fonts.body, size: 15, lineHeight: 22, maxScale: 2 },
  /** Form labels, chips, list rows. */
  label: { family: fonts.bodySemibold, size: 13, lineHeight: 18, maxScale: 1.8 },
  /** Footnotes and hints. */
  caption: { family: fonts.body, size: 12, lineHeight: 17, maxScale: 1.8 },
  /** Uppercase eyebrow above a title. */
  kicker: { family: fonts.monoMedium, size: 11, lineHeight: 14, letterSpacing: 1.2, maxScale: 1.4 },
  /** Button and control labels. */
  button: { family: fonts.bodyBold, size: 15, lineHeight: 20, maxScale: 1.5 },
  /** Hashes, paths, sizes. */
  mono: { family: fonts.mono, size: 12, lineHeight: 17, maxScale: 1.6 },
  /** Lives in a fixed-height ruler; the tightest cap in the system. */
  timecode: { family: fonts.monoMedium, size: 13, lineHeight: 16, maxScale: 1.3 },
} as const satisfies Record<string, TypeStyle>;

export type TypeName = keyof typeof type;

// ---------------------------------------------------------------------------
// Space, shape, motion
// ---------------------------------------------------------------------------

/** A 4pt rhythm with deliberate gaps: there is no 20 or 28, so spacing has to
 *  step rather than drift into "whatever looked right in this one card". */
export const space = { 1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 7: 48, 8: 64 } as const;

export const radius = { xs: 4, sm: 8, md: 12, lg: 18, pill: 999 } as const;

/** Apple's floor for a touch target. Every Pressable in the kit meets it. */
export const HIT_SLOP_MIN = 44;

/**
 * Motion. Only `opacity` and `transform` are ever animated — those are the two
 * the compositor can run off the JS thread, and a timeline that stutters while
 * a spinner animates is the whole reason for the rule.
 */
export const motion = {
  fast: 130,
  normal: 220,
  slow: 380,
  /** cubic-bezier(0.16, 1, 0.3, 1) — decelerate hard, settle without bounce. */
  easeOutExpo: [0.16, 1, 0.3, 1] as const,
} as const;

/**
 * True when the user has asked iOS to reduce motion (Settings › Accessibility ›
 * Motion). Every animated call site in the app reads this and drops to an
 * instant state change rather than a transition — a companion for a video
 * editor is used by people who are already looking at moving pictures, and an
 * app that ignores the switch is the one that makes them put the phone down.
 *
 * Lives here rather than in a hooks file because it is a design token in
 * practice: it is the "duration = 0" branch of `motion` above.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    let alive = true;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((on) => {
        if (alive) setReduced(on);
      })
      // The query can reject while the app is backgrounded. Full motion is the
      // safe default here (it is what the user sees today), and the listener
      // below still corrects us the moment the setting is read successfully.
      .catch(() => undefined);

    const sub = AccessibilityInfo.addEventListener("reduceMotionChanged", (on) => setReduced(on));
    return () => {
      alive = false;
      sub.remove();
    };
  }, []);

  return reduced;
}

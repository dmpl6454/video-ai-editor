/**
 * The design system's contract.
 *
 * Contrast is the one design property that is objectively checkable, and it is
 * also the one that silently rots: someone lightens a surface to "soften" a
 * card and takes three text pairings under the line without noticing. This
 * file walks `contrastPairs`, which is DERIVED from `tint` and `fill` rather
 * than hand-listed, so a new tone cannot be added without arriving here.
 */

import { color, contrastPairs, fill, tint, type } from "../../constants/theme";
import { contrastRatio } from "../helpers/contrast";

/** WCAG AA for normal-sized text. The kit has no text above 24px bold, so the
 *  3:1 large-text allowance is deliberately never claimed. */
const AA = 4.5;

describe("contrast", () => {
  it("clears 4.5:1 for every pairing the component kit can render", () => {
    const failures = contrastPairs
      .map((p) => ({ ...p, ratio: contrastRatio(p.fg, p.bg) }))
      .filter((p) => p.ratio < AA)
      .map((p) => `${p.use}: ${p.fg} on ${p.bg} = ${p.ratio.toFixed(2)}:1`);
    expect(failures).toEqual([]);
  });

  it("covers the pairings named in the design spec", () => {
    // A guard on the guard: if `contrastPairs` were ever emptied or narrowed,
    // the test above would pass vacuously. These are asserted by hand.
    for (const bg of [color.bg0, color.bg1, color.bg2]) {
      expect(contrastRatio(color.text, bg)).toBeGreaterThanOrEqual(AA);
      expect(contrastRatio(color.textDim, bg)).toBeGreaterThanOrEqual(AA);
    }
    // Field errors are warn-on-page and warn-in-card.
    expect(contrastRatio(color.accent.warn, color.bg0)).toBeGreaterThanOrEqual(AA);
    expect(contrastRatio(color.accent.warn, color.bg1)).toBeGreaterThanOrEqual(AA);
    // good and secondary carry text on the page, in cards, and on their tints.
    for (const fg of [color.accent.good, color.accent.secondary]) {
      expect(contrastRatio(fg, color.bg0)).toBeGreaterThanOrEqual(AA);
      expect(contrastRatio(fg, color.bg1)).toBeGreaterThanOrEqual(AA);
    }
    expect(contrastRatio(tint.good.fg, tint.good.bg)).toBeGreaterThanOrEqual(AA);
    expect(contrastRatio(tint.info.fg, tint.info.bg)).toBeGreaterThanOrEqual(AA);
  });

  it("has a non-trivial number of pairings", () => {
    expect(contrastPairs.length).toBeGreaterThan(20);
  });
});

describe("the brand accent is a fill, never a text colour", () => {
  /**
   * `#FF4D6D` on `#0E0E10` actually measures 6.00:1, so it would pass as text.
   * The rule is a SYSTEM decision rather than a contrast one: the brand pink
   * marks the single primary action on a screen, and text in the same colour
   * makes that action impossible to find. The pairing it really rules out is
   * the desktop's `button.primary { color: #fff }` — white on the pink is
   * 3.21:1, and this is where the phone diverges from it.
   */
  it("never appears as a foreground in any renderable pairing", () => {
    const asText = contrastPairs.filter((p) => p.fg.toLowerCase() === color.accent.primary.toLowerCase());
    expect(asText).toEqual([]);
  });

  it("is not the foreground of any tint or fill", () => {
    for (const t of Object.values(tint)) {
      expect(t.fg.toLowerCase()).not.toBe(color.accent.primary.toLowerCase());
    }
    for (const f of Object.values(fill)) {
      expect(f.fg.toLowerCase()).not.toBe(color.accent.primary.toLowerCase());
    }
  });

  it("still carries legible ink when used as a fill", () => {
    expect(contrastRatio(fill.primary.fg, fill.primary.bg)).toBeGreaterThanOrEqual(AA);
    // The specific pairing the desktop ships and the phone refuses.
    expect(contrastRatio("#ffffff", color.accent.primary)).toBeLessThan(AA);
  });
});

describe("danger", () => {
  it("is a different colour from the primary action", () => {
    expect(color.danger).not.toBe(color.accent.primary);
  });

  it("is legible as text on both grounds and as a fill", () => {
    expect(contrastRatio(color.danger, color.bg0)).toBeGreaterThanOrEqual(AA);
    expect(contrastRatio(color.danger, color.bg1)).toBeGreaterThanOrEqual(AA);
    expect(contrastRatio(fill.danger.fg, fill.danger.bg)).toBeGreaterThanOrEqual(AA);
  });
});

describe("type scale", () => {
  it("gives every entry a maxScale", () => {
    for (const [name, style] of Object.entries(type)) {
      expect(typeof style.maxScale).toBe("number");
      expect(style.maxScale).toBeGreaterThan(1);
      // Above ~2× a full-width line of body copy no longer fits a phone at
      // all; the cap exists to stop clipping, not to defeat Dynamic Type.
      expect(style.maxScale).toBeLessThanOrEqual(2);
      expect(name).toBeTruthy();
    }
  });

  it("keeps every line height above its font size", () => {
    for (const style of Object.values(type)) {
      expect(style.lineHeight).toBeGreaterThan(style.size);
    }
  });

  it("caps denser roles more tightly than prose", () => {
    // Timecode lives in a fixed-height ruler; body reflows and may grow most.
    expect(type.timecode.maxScale).toBeLessThan(type.body.maxScale);
    expect(type.display.maxScale).toBeLessThan(type.body.maxScale);
  });
});

describe("token hygiene", () => {
  it("uses six-digit hex everywhere a colour is a plain value", () => {
    const flat = [
      color.bg0, color.bg1, color.bg2, color.bg3, color.line, color.lineStrong,
      color.text, color.textDim, color.danger, color.dangerSoft, color.dangerInk,
      ...Object.values(color.accent), ...Object.values(color.track), color.selection,
    ];
    for (const c of flat) expect(c).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("keeps the two text tones distinct", () => {
    expect(color.text).not.toBe(color.textDim);
    expect(contrastRatio(color.text, color.textDim)).toBeGreaterThan(1.5);
  });
});

// The Adjustment tab's −100…100 sliders ↔ the engine's color_grade ranges.
/** Slider −100…100 ↔ the engine's ranges (lib-free, so it stays testable). */
export const grade = {
  toSlider: {
    brightness: (v: number) => Math.round(v * 200),                 // −0.5…0.5
    contrast: (v: number) => Math.round(v >= 1 ? (v - 1) * 100 : (v - 1) * 200), // 0.5…2
    saturation: (v: number) => Math.round(v >= 1 ? (v - 1) * 50 : (v - 1) * 100), // 0…3
    temp: (v: number) => Math.round(v * 100),
    tint: (v: number) => Math.round(v * 100),
  },
  fromSlider: {
    brightness: (s: number) => s / 200,
    contrast: (s: number) => (s >= 0 ? 1 + s / 100 : 1 + s / 200),
    saturation: (s: number) => (s >= 0 ? 1 + s / 50 : 1 + s / 100),
    temp: (s: number) => s / 100,
    tint: (s: number) => s / 100,
  },
}


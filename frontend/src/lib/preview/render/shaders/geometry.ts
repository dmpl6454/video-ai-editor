// GLSL ES 3.00 sources of the Phase 1 geometry pass (spec §3.4, §9.1
// `render/shaders/geometry.ts`). One full-screen triangle; the fragment
// shader walks, per output pixel, the INVERSE chain geometry.ts computes:
//
//   display px → EDL canvas px → F2 (before transform + flip) → F1 (before
//   rotation) → source uv
//
// painting black wherever a stage's bounds say the export has no picture
// (letterbox pad, rotated-away corners, a static pan's margin), and
// multiplying RGB by the clip's gain (opacity × video fades — both fade the
// export's pixels toward black). A gap frame is drawn with u_black = 1.

export const GEOMETRY_VERT = `#version 300 es
// A single triangle that covers clip space: no vertex buffer needed.
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
`

export const GEOMETRY_FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_tex;
uniform vec2 u_dispToCanvas;   // EDL canvas px per display px
uniform float u_dispH;         // display height (gl_FragCoord is bottom-up)
uniform mat3 u_toF2;           // canvas px -> F2
uniform vec4 u_f2Bounds;       // x0 y0 x1 y1
uniform mat3 u_toF1;           // F2 -> F1
uniform vec4 u_f1Bounds;
uniform int u_f1Clamp;         // rotate: bilinear over the F1 pixel grid, edge-clamped
uniform vec4 u_f1Extent;       // 0 0 W H
uniform mat3 u_toUv;           // F1 -> source uv (y down)
uniform vec4 u_uvBounds;
uniform float u_gain;
uniform int u_black;
uniform float u_lod;           // mip level of one F1 pixel's footprint
out vec4 outColor;

bool inside(vec2 p, vec4 b) {
  return p.x >= b.x && p.y >= b.y && p.x <= b.z && p.y <= b.w;
}

// The picture at F1 point q (black outside the fitted picture).
vec3 atF1(vec2 q) {
  vec2 uv = (u_toUv * vec3(q, 1.0)).xy;
  if (!inside(uv, u_uvBounds)) return vec3(0.0);
  return textureLod(u_tex, uv, u_lod).rgb;
}

void main() {
  if (u_black == 1) { outColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
  vec2 disp = vec2(gl_FragCoord.x, u_dispH - gl_FragCoord.y);
  vec2 p = disp * u_dispToCanvas;
  vec2 f2 = (u_toF2 * vec3(p, 1.0)).xy;
  if (!inside(f2, u_f2Bounds)) { outColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
  vec2 f1 = (u_toF1 * vec3(f2, 1.0)).xy;
  if (!inside(f1, u_f1Bounds)) { outColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
  vec3 c;
  if (u_f1Clamp == 1) {
    // vf_rotate resamples the already-fitted canvas-sized frame with
    // bilinear interpolation between its PIXELS (indices clamped to the
    // edge): do the same over the F1 pixel grid, each grid pixel taken from
    // the texture, so the rotated picture carries the export's softness.
    vec2 g = f1 - 0.5;
    vec2 i0 = floor(g);
    vec2 fr = g - i0;
    vec2 lo = u_f1Extent.xy + 0.5;
    vec2 hi = u_f1Extent.zw - 0.5;
    vec3 c00 = atF1(clamp(i0 + vec2(0.5, 0.5), lo, hi));
    vec3 c10 = atF1(clamp(i0 + vec2(1.5, 0.5), lo, hi));
    vec3 c01 = atF1(clamp(i0 + vec2(0.5, 1.5), lo, hi));
    vec3 c11 = atF1(clamp(i0 + vec2(1.5, 1.5), lo, hi));
    c = mix(mix(c00, c10, fr.x), mix(c01, c11, fr.x), fr.y);
  } else {
    vec2 uv = (u_toUv * vec3(f1, 1.0)).xy;
    if (!inside(uv, u_uvBounds)) { outColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
    c = texture(u_tex, uv).rgb;
  }
  outColor = vec4(clamp(c * u_gain, 0.0, 1.0), 1.0);
}
`

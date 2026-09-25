"""HDR (PQ / HLG, BT.2020) → SDR BT.709 tone mapping without libzimg.

WHY THIS EXISTS (QA-042). The ingest normaliser used to try `zscale+tonemap`
(needs libzimg, which the Homebrew `ffmpeg` formula does not build — only
`ffmpeg-full` does) and then `colorspace=all=bt709`, which REJECTS the
smpte2084 transfer outright. Both failed on a stock Mac, so every HDR clip
fell through to a plain `-pix_fmt yuv420p` decode: 8-bit pixels that still
held PQ code values, still tagged BT.2020/SMPTE-2084. Players then showed it
either washed out (ignoring the tag) or as HDR that was no longer HDR.

The fix is a 3D LUT generated here with numpy and applied with ffmpeg's
built-in `lut3d` filter, which every ffmpeg build has. The chain is

    YUV (bt2020nc, limited) --swscale--> RGB' (PQ or HLG encoded, BT.2020)
        --lut3d--> RGB' (BT.709 OETF, BT.709 primaries)
        --swscale--> YUV (bt709, limited) + explicit bt709 tags

and the LUT does, per grid point: EOTF to display light in nits, a
luminance-preserving tone curve that keeps everything up to 70 % of SDR white
LINEAR (so skin and mid-tones are untouched) and rolls highlights off smoothly
to the assumed 1000-nit mastering peak, a BT.2020 → BT.709 gamut matrix, and
the BT.709 OETF. 203 nits (BT.2408 HDR reference white) lands at ~92 % of the
SDR code range rather than at the ~58 % a pass-through leaves it.

The LUT is sampled in the ENCODED (perceptual) domain, so 33 points per axis
are enough; tetrahedral interpolation keeps the error well under one 8-bit
code value on grey ramps.
"""
from __future__ import annotations

from pathlib import Path

#: Transfer names ffprobe reports for the two HDR systems we convert.
PQ_TRANSFERS = frozenset({"smpte2084"})
HLG_TRANSFERS = frozenset({"arib-std-b67"})

#: BT.2408 HDR reference white, the level SDR 100 % corresponds to.
REFERENCE_WHITE_NITS = 203.0
#: Assumed mastering peak when the stream carries no metadata we read.
MASTERING_PEAK_NITS = 1000.0
#: Below this fraction of SDR white the tone curve is the identity.
_KNEE = 0.70
#: Grid points per axis.
LUT_SIZE = 33

# Linear-light BT.2020 → BT.709 primaries (ITU-R BT.2087).
_BT2020_TO_BT709 = (
    (1.6605, -0.5876, -0.0728),
    (-0.1246, 1.1329, -0.0083),
    (-0.0182, -0.1006, 1.1187),
)
_BT2020_LUMA = (0.2627, 0.6780, 0.0593)


def _pq_to_nits(e):
    import numpy as np
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    ep = np.power(np.clip(e, 0.0, 1.0), 1.0 / m2)
    lin = np.power(np.maximum(ep - c1, 0.0) / (c2 - c3 * ep), 1.0 / m1)
    return 10000.0 * lin


def _hlg_to_nits(rgb):
    """HLG signal → display light (nits) through the BT.2100 OOTF at a 1000-nit
    nominal peak (system gamma 1.2)."""
    import numpy as np
    a = 0.17883277
    b = 1 - 4 * a
    c = 0.5 - a * np.log(4 * a)
    e = np.clip(rgb, 0.0, 1.0)
    scene = np.where(e <= 0.5, e * e / 3.0, (np.exp((e - c) / a) + b) / 12.0)
    ys = scene @ np.array(_BT2020_LUMA)
    gain = np.power(np.maximum(ys, 1e-9), 1.2 - 1.0)
    return MASTERING_PEAK_NITS * scene * gain[..., None]


def _tone_curve(x, peak):
    """SDR-relative luminance (1.0 = reference white) → SDR linear 0..1.

    Identity up to the knee, then an extended-Reinhard shoulder that is C1 at
    the knee and reaches exactly 1.0 at `peak`."""
    import numpy as np
    k = _KNEE
    t = np.maximum(x - k, 0.0) / (1.0 - k)
    tmax = max((peak - k) / (1.0 - k), 1e-6)
    shoulder = k + (1.0 - k) * t * (1.0 + t / (tmax * tmax)) / (1.0 + t)
    return np.where(x <= k, x, np.minimum(shoulder, 1.0))


def _bt709_oetf(lin):
    import numpy as np
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin < 0.018, 4.5 * lin, 1.099 * np.power(lin, 0.45) - 0.099)


def hdr_to_sdr_rgb(rgb, transfer: str):
    """Map encoded BT.2020 HDR R'G'B' (…, 3) in 0..1 to encoded BT.709 SDR R'G'B'.

    Pure numpy, exposed so the tests can check the curve without ffmpeg."""
    import numpy as np
    rgb = np.asarray(rgb, dtype=np.float64)
    nits = _hlg_to_nits(rgb) if transfer in HLG_TRANSFERS else _pq_to_nits(rgb)
    rel = nits / REFERENCE_WHITE_NITS
    y = rel @ np.array(_BT2020_LUMA)
    peak = MASTERING_PEAK_NITS / REFERENCE_WHITE_NITS
    y_out = _tone_curve(y, peak)
    scale = np.where(y > 1e-9, y_out / np.maximum(y, 1e-9), 0.0)
    sdr_2020 = rel * scale[..., None]
    sdr_709 = sdr_2020 @ np.array(_BT2020_TO_BT709).T
    # Out-of-gamut colours: clip negatives, then pull anything still above 1
    # back toward its own luminance rather than hard-clipping a channel (which
    # would shift hue).
    sdr_709 = np.maximum(sdr_709, 0.0)
    over = np.max(sdr_709, axis=-1)
    sdr_709 = np.where(over[..., None] > 1.0, sdr_709 / np.maximum(over, 1.0)[..., None], sdr_709)
    return _bt709_oetf(sdr_709)


def write_tonemap_lut(transfer: str, dst: Path, size: int = LUT_SIZE) -> Path:
    """Write a `.cube` 3D LUT for `transfer` (smpte2084 or arib-std-b67) to
    `dst`. Red varies fastest, as the .cube format and ffmpeg's lut3d expect."""
    import numpy as np
    axis = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    out = hdr_to_sdr_rgb(grid, transfer)
    lines = [f"TITLE \"{transfer} to bt709 sdr\"", f"LUT_3D_SIZE {size}",
             "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in out]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dst


def tonemap_chain(lut_path: str) -> str:
    """The filtergraph fragment that applies the LUT (``lut_path`` must already
    be escaped with ``platformutil.ffmpeg_filter_path``) and tags the result."""
    return (
        "scale=in_color_matrix=bt2020:in_range=tv:out_range=pc,format=gbrp16le,"
        f"lut3d=file={lut_path}:interp=tetrahedral,"
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
    )


#: Output options that write the BT.709 tags into the H.264 VUI.
BT709_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]


__all__ = ["PQ_TRANSFERS", "HLG_TRANSFERS", "hdr_to_sdr_rgb", "write_tonemap_lut",
           "tonemap_chain", "BT709_TAGS"]

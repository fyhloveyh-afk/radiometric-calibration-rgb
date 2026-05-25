#!/usr/bin/env python3
"""Check whether a RAW image is overexposed.

The script can read a companion JSON written by the capture program, so for
normal use you only need to pass the RAW file path.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

import numpy as np


BAYER_PATTERNS = {"rggb", "bggr", "grbg", "gbrg"}
EXPOSURE_RE = re.compile(r"(?:^|_)(?P<exposure>\d+(?:\.\d+)?)ms(?:_|$)", re.IGNORECASE)


def companion_json_path(raw_path: Path) -> Optional[Path]:
    candidates = [
        raw_path.with_suffix(".json"),
        raw_path.with_name(raw_path.stem + "_raw.json"),
    ]
    if raw_path.stem.endswith("_raw"):
        candidates.append(raw_path.with_name(raw_path.stem[:-4] + "_raw.json"))
    for path in candidates:
        if path.exists():
            return path
    return None


def load_metadata(raw_path: Path) -> dict[str, object]:
    json_path = companion_json_path(raw_path)
    if json_path is None:
        return {}
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with json_path.open("r", encoding=encoding) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    with json_path.open("r", encoding="utf-8", errors="replace") as f:
        return json.load(f)


def parse_roi(text: Optional[str]) -> Optional[tuple[int, int, int, int]]:
    if not text:
        return None
    parts = [int(p.strip()) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height")
    x, y, w, h = parts
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("ROI values out of range")
    return x, y, w, h


def infer_bayer_pattern(meta: dict[str, object], fallback: str) -> str:
    media_type = str(meta.get("mediaType", "")).upper()
    if "BAYGB" in media_type:
        return "gbrg"
    if "BAYRG" in media_type:
        return "rggb"
    if "BAYGR" in media_type:
        return "grbg"
    if "BAYBG" in media_type:
        return "bggr"
    return fallback


def exposure_ms_from_path_or_meta(raw_path: Path, meta: dict[str, object]) -> Optional[float]:
    if "exposure_us" in meta:
        return float(meta["exposure_us"]) / 1000.0
    match = EXPOSURE_RE.search(raw_path.stem)
    if match:
        return float(match.group("exposure"))
    return None


def normalize_effective_dn(image: np.ndarray, effective_bits: int) -> tuple[np.ndarray, str]:
    """Return DN in effective bit depth.

    Some SDKs store 12-bit data left-aligned in a uint16 container. In that
    case raw values are multiples of 16 and can reach about 65504, so shifting
    right by 4 recovers the true 12-bit DN.
    """

    dtype_bits = image.dtype.itemsize * 8
    if np.issubdtype(image.dtype, np.unsignedinteger) and dtype_bits > effective_bits:
        shift = dtype_bits - effective_bits
        max_effective = (1 << effective_bits) - 1
        if int(image.max()) > max_effective:
            return (image >> shift).astype(np.uint16), f"right-shifted {shift} bits from {dtype_bits}-bit container"
    return image, "native effective DN"


def bayer_channel_masks(shape: tuple[int, int], pattern: str, roi_origin: tuple[int, int]) -> dict[str, np.ndarray]:
    height, width = shape
    x0, y0 = roi_origin
    yy, xx = np.indices((height, width))
    abs_y = yy + y0
    abs_x = xx + x0
    color_at = {
        (0, 0): pattern[0],
        (0, 1): pattern[1],
        (1, 0): pattern[2],
        (1, 1): pattern[3],
    }
    masks: dict[str, np.ndarray] = {}
    for color in ("r", "g", "b"):
        mask = np.zeros((height, width), dtype=bool)
        for yp in (0, 1):
            for xp in (0, 1):
                if color_at[(yp, xp)] == color:
                    mask |= (abs_y % 2 == yp) & (abs_x % 2 == xp)
        masks[color] = mask
    return masks


def summarize(
    name: str,
    samples: np.ndarray,
    max_dn: int,
    near_dn: int,
    full_scale_dn: int,
    peak_percentile: float,
) -> dict[str, object]:
    total = int(samples.size)
    saturated = int(np.count_nonzero(samples >= max_dn))
    near_saturated = int(np.count_nonzero(samples >= near_dn))
    max_value = int(samples.max()) if total else None
    mean_value = float(samples.mean()) if total else None
    p99_value = float(np.percentile(samples, 99)) if total else None
    peak_value = float(np.percentile(samples, peak_percentile)) if total else None
    return {
        "channel": name,
        "pixels": total,
        "max": max_value,
        "max_full_scale_percent": max_value / full_scale_dn * 100.0 if total else 0.0,
        "mean": mean_value,
        "mean_full_scale_percent": mean_value / full_scale_dn * 100.0 if total else 0.0,
        "p99": p99_value,
        "p99_full_scale_percent": p99_value / full_scale_dn * 100.0 if total else 0.0,
        "peak_percentile": peak_percentile,
        "peak_value": peak_value,
        "peak_full_scale_percent": peak_value / full_scale_dn * 100.0 if total else 0.0,
        "saturated_pixels": saturated,
        "saturated_percent": saturated / total * 100.0 if total else 0.0,
        "near_saturated_pixels": near_saturated,
        "near_saturated_percent": near_saturated / total * 100.0 if total else 0.0,
    }


def format_row(row: dict[str, object]) -> str:
    return (
        f"{str(row['channel']).upper():>5}  "
        f"pixels={int(row['pixels']):>9}  "
        f"max={int(row['max']):>5}  "
        f"max_full={float(row['max_full_scale_percent']):>6.2f}%  "
        f"p99={float(row['p99']):>8.1f}  "
        f"p{float(row['peak_percentile']):g}_full={float(row['peak_full_scale_percent']):>6.2f}%  "
        f"sat={int(row['saturated_pixels']):>8} ({float(row['saturated_percent']):>7.4f}%)  "
        f"near={int(row['near_saturated_pixels']):>8} ({float(row['near_saturated_percent']):>7.4f}%)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check RAW overexposure by channel.")
    parser.add_argument("raw_file", type=Path, help="RAW file path")
    parser.add_argument("--width", type=int, help="image width; auto-read from companion JSON when omitted")
    parser.add_argument("--height", type=int, help="image height; auto-read from companion JSON when omitted")
    parser.add_argument("--dtype", default=None, help="RAW storage dtype, e.g. uint8 or uint16")
    parser.add_argument("--bit-depth", type=int, choices=[8, 12], help="camera effective bit depth: 8 or 12")
    parser.add_argument("--effective-bits", type=int, help="effective bit depth, e.g. 8 or 12")
    parser.add_argument("--channels", type=int, default=1, help="1 for Bayer/mono RAW, 3 for interleaved RGB")
    parser.add_argument("--raw-format", choices=["bayer", "rgb", "mono"], default="bayer")
    parser.add_argument("--bayer-pattern", default="gbrg", choices=sorted(BAYER_PATTERNS))
    parser.add_argument("--roi", type=parse_roi, help="optional ROI as x,y,width,height")
    parser.add_argument("--near-percent", type=float, default=99.0, help="near saturation threshold percent of max DN")
    parser.add_argument("--max-dn", type=int, help="override saturation DN; default is 2^effective_bits - 1")
    parser.add_argument("--peak-percentile", type=float, default=99.9, help="robust bright-pixel percentile for exposure usage")
    parser.add_argument("--min-useful-percent", type=float, default=20.0, help="below this bright-pixel usage is reported as too low")
    parser.add_argument("--target-percent", type=float, default=80.0, help="target bright-pixel usage for exposure-time hint")
    args = parser.parse_args()

    raw_path = args.raw_file
    if not raw_path.exists():
        raise SystemExit(f"RAW file not found: {raw_path}")

    meta = load_metadata(raw_path)
    width = args.width or int(meta.get("width", 0) or 0)
    height = args.height or int(meta.get("height", 0) or 0)
    if width <= 0 or height <= 0:
        raise SystemExit("Width/height are required when no companion JSON is available.")

    effective_bits = args.bit_depth or args.effective_bits or int(
        meta.get("effectiveBitDepth", meta.get("bitDepth", 8 if args.dtype == "uint8" else 12))
    )
    if args.dtype:
        dtype_name = args.dtype
    elif args.bit_depth == 8:
        dtype_name = "uint8"
    else:
        dtype_name = "uint8" if int(meta.get("storageBitDepth", 16)) <= 8 else "uint16"
    dtype = np.dtype(dtype_name)
    theoretical_max_dn = (1 << effective_bits) - 1
    max_dn = args.max_dn if args.max_dn is not None else theoretical_max_dn
    near_dn = int(round(max_dn * args.near_percent / 100.0))

    data = np.fromfile(raw_path, dtype=dtype)
    expected = width * height * args.channels
    if data.size != expected:
        raise SystemExit(f"Size mismatch: expected {expected} samples, got {data.size}. Check width/height/dtype/channels.")

    if args.channels == 1:
        image = data.reshape(height, width)
    else:
        image = data.reshape(height, width, args.channels)

    roi = args.roi
    roi_origin = (0, 0)
    if roi is not None:
        x, y, w, h = roi
        if x + w > width or y + h > height:
            raise SystemExit(f"ROI {roi} exceeds image size {width}x{height}.")
        image = image[y : y + h, x : x + w]
        roi_origin = (x, y)

    image_eff, normalize_note = normalize_effective_dn(image, effective_bits)
    bayer_pattern = infer_bayer_pattern(meta, args.bayer_pattern)
    observed_max = int(image_eff.max())
    saturation_dn = max_dn
    if args.max_dn is None and effective_bits == 12 and observed_max == theoretical_max_dn - 1:
        saturation_dn = observed_max
        near_dn = int(round(saturation_dn * args.near_percent / 100.0))

    if not (0.0 < args.peak_percentile <= 100.0):
        raise SystemExit("--peak-percentile must be in (0, 100].")
    if not (0.0 < args.min_useful_percent < 100.0):
        raise SystemExit("--min-useful-percent must be in (0, 100).")
    if not (0.0 < args.target_percent < 100.0):
        raise SystemExit("--target-percent must be in (0, 100).")

    rows: list[dict[str, object]] = []
    rows.append(
        summarize(
            "all",
            image_eff.reshape(-1),
            saturation_dn,
            near_dn,
            theoretical_max_dn,
            args.peak_percentile,
        )
    )

    if args.raw_format == "rgb":
        for idx, channel in enumerate(("r", "g", "b")):
            rows.append(
                summarize(
                    channel,
                    image_eff[:, :, idx].reshape(-1),
                    saturation_dn,
                    near_dn,
                    theoretical_max_dn,
                    args.peak_percentile,
                )
            )
    elif args.raw_format == "bayer":
        masks = bayer_channel_masks(image_eff.shape[:2], bayer_pattern, roi_origin)
        for channel in ("r", "g", "b"):
            rows.append(
                summarize(
                    channel,
                    image_eff[masks[channel]],
                    saturation_dn,
                    near_dn,
                    theoretical_max_dn,
                    args.peak_percentile,
                )
            )

    print(f"RAW: {raw_path}")
    if meta:
        json_path = companion_json_path(raw_path)
        print(f"JSON: {json_path}")
    print(f"size: {width} x {height}, dtype: {dtype_name}, effective_bits: {effective_bits}")
    print(f"normalization: {normalize_note}")
    print(f"theoretical_max_dn: {theoretical_max_dn}, saturation_dn_used: {saturation_dn}")
    print(f"near_threshold: >= {near_dn} ({args.near_percent:g}% of saturation DN)")
    print(
        f"low-exposure check: max_full and p{args.peak_percentile:g}_full show bright-pixel "
        "usage of the effective full scale"
    )
    if roi is not None:
        print(f"ROI: {roi[0]},{roi[1]},{roi[2]},{roi[3]}")
    if args.raw_format == "bayer":
        print(f"Bayer pattern: {bayer_pattern}")
    print()
    print("channel statistics:")
    for row in rows:
        print(format_row(row))

    over_channels = [row for row in rows if row["channel"] != "all" and int(row["saturated_pixels"]) > 0]
    near_channels = [row for row in rows if row["channel"] != "all" and int(row["near_saturated_pixels"]) > 0]
    channel_rows = [row for row in rows if row["channel"] != "all"]
    brightest_row = max(channel_rows, key=lambda row: float(row["max_full_scale_percent"])) if channel_rows else rows[0]
    brightest_robust_row = (
        max(channel_rows, key=lambda row: float(row["peak_full_scale_percent"])) if channel_rows else rows[0]
    )
    exposure_ms = exposure_ms_from_path_or_meta(raw_path, meta)
    print()
    if over_channels:
        labels = ", ".join(str(row["channel"]).upper() for row in over_channels)
        print(f"Result: OVEREXPOSED. Saturated channel(s): {labels}")
    elif near_channels:
        labels = ", ".join(str(row["channel"]).upper() for row in near_channels)
        print(f"Result: near saturation but not clipped at max DN. Near-saturated channel(s): {labels}")
    else:
        print("Result: not overexposed by the selected threshold.")

    print()
    print(
        "Brightest-pixel usage: "
        f"{str(brightest_row['channel']).upper()} max uses "
        f"{float(brightest_row['max_full_scale_percent']):.2f}% of full scale."
    )
    print(
        f"Robust bright usage: {str(brightest_robust_row['channel']).upper()} "
        f"p{args.peak_percentile:g} uses "
        f"{float(brightest_robust_row['peak_full_scale_percent']):.2f}% of full scale."
    )

    robust_usage = float(brightest_robust_row["peak_full_scale_percent"])
    if over_channels:
        print("Exposure hint: reduce exposure; clipped pixels cannot give a reliable linear scaling estimate.")
    elif robust_usage < args.min_useful_percent:
        print(
            f"Exposure hint: signal is low for calibration by the p{args.peak_percentile:g} criterion "
            f"(< {args.min_useful_percent:g}% full scale)."
        )
    elif robust_usage >= args.near_percent:
        print("Exposure hint: very close to saturation; use a shorter exposure for calibration.")
    else:
        print("Exposure hint: usable dynamic-range occupancy for calibration.")

    if exposure_ms is not None and robust_usage > 0 and not over_channels:
        target_exposure_ms = exposure_ms * args.target_percent / robust_usage
        print(
            f"Assuming linear response, exposure for about {args.target_percent:g}% "
            f"p{args.peak_percentile:g} full scale: {target_exposure_ms:.3f} ms "
            f"(current {exposure_ms:.3f} ms)."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

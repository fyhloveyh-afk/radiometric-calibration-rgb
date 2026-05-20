#!/usr/bin/env python3
"""Diagnose RAW ROI saturation for calibration captures."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import numpy as np


LIGHT_RE = re.compile(r"^(?P<temp>-?\d+(?:\.\d+)?)_(?P<exp>\d+(?:\.\d+)?)_(?P<rep>\d+)$")
DARK_RE = re.compile(r"^(?:dark_)?(?P<exp>\d+(?:\.\d+)?)_(?P<rep>\d+)$", re.IGNORECASE)


def parse_roi(text: str) -> tuple[int, int, int, int]:
    parts = [int(p.strip()) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height")
    x, y, w, h = parts
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("ROI values out of range")
    return x, y, w, h


def parse_pattern(text: str) -> str:
    value = text.strip().lower()
    if value not in {"rggb", "bggr", "grbg", "gbrg"}:
        raise argparse.ArgumentTypeError("Bayer pattern must be rggb/bggr/grbg/gbrg")
    return value


def iter_raw(folder: Path, ext: str) -> list[Path]:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix.lower())


def read_roi(path: Path, width: int, height: int, dtype: str, roi: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = roi
    data = np.fromfile(path, dtype=np.dtype(dtype))
    expected = width * height
    if data.size != expected:
        raise ValueError(f"{path.name}: expected {expected} samples, got {data.size}")
    image = data.reshape(height, width)
    if x + w > width or y + h > height:
        raise ValueError(f"{path.name}: ROI {roi} exceeds image size {width}x{height}")
    return image[y : y + h, x : x + w]


def channel_samples(roi_image: np.ndarray, roi_xy: tuple[int, int], pattern: str) -> dict[str, np.ndarray]:
    x0, y0 = roi_xy
    color_at = {
        (0, 0): pattern[0],
        (0, 1): pattern[1],
        (1, 0): pattern[2],
        (1, 1): pattern[3],
    }
    parts: dict[str, list[np.ndarray]] = {"r": [], "g": [], "b": []}
    for row_parity in (0, 1):
        for col_parity in (0, 1):
            color = color_at[((y0 + row_parity) % 2, (x0 + col_parity) % 2)]
            samples = roi_image[row_parity::2, col_parity::2].reshape(-1)
            if samples.size:
                parts[color].append(samples)
    return {color: np.concatenate(values) for color, values in parts.items() if values}


def summarize(samples: np.ndarray, sat_value: int, near_sat_value: int) -> dict[str, float]:
    return {
        "mean": float(samples.mean()),
        "min": float(samples.min()),
        "max": float(samples.max()),
        "p99": float(np.percentile(samples, 99)),
        "sat_pct": float((samples >= sat_value).mean() * 100.0),
        "near_sat_pct": float((samples >= near_sat_value).mean() * 100.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--dark-dir", type=Path)
    parser.add_argument("--roi", required=True, type=parse_roi)
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--dtype", default="uint16")
    parser.add_argument("--bayer-pattern", default="gbrg", type=parse_pattern)
    parser.add_argument("--raw-ext", default=".raw")
    parser.add_argument("--sat-value", type=int)
    parser.add_argument("--near-sat-value", type=int)
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    dtype = np.dtype(args.dtype)
    sat_value = args.sat_value if args.sat_value is not None else int(np.iinfo(dtype).max)
    near_sat_value = args.near_sat_value if args.near_sat_value is not None else int(sat_value * 0.95)

    rows: list[dict[str, object]] = []

    def handle_file(path: Path, kind: str, temp: float | None, exp: float, rep: int) -> None:
        roi_image = read_roi(path, args.width, args.height, args.dtype, args.roi)
        samples_by_channel = channel_samples(roi_image, (args.roi[0], args.roi[1]), args.bayer_pattern)
        for channel, samples in samples_by_channel.items():
            stats = summarize(samples, sat_value, near_sat_value)
            rows.append(
                {
                    "kind": kind,
                    "temperature_c": "" if temp is None else temp,
                    "exposure_ms": exp,
                    "repeat": rep,
                    "channel": channel,
                    "file": str(path),
                    **stats,
                }
            )

    for path in iter_raw(args.image_dir, args.raw_ext):
        match = LIGHT_RE.match(path.stem)
        if match:
            handle_file(path, "light", float(match.group("temp")), float(match.group("exp")), int(match.group("rep")))

    if args.dark_dir is not None and args.dark_dir.exists():
        for path in iter_raw(args.dark_dir, args.raw_ext):
            match = DARK_RE.match(path.stem)
            if match:
                handle_file(path, "dark", None, float(match.group("exp")), int(match.group("rep")))

    if not rows:
        raise SystemExit("No matching RAW files found.")

    out_csv = args.output_csv or (args.image_dir.parent / "raw_saturation_report.csv")
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {out_csv}")
    print("Saturation summary by light exposure:")
    for exp in sorted({float(r["exposure_ms"]) for r in rows if r["kind"] == "light"}):
        exp_rows = [r for r in rows if r["kind"] == "light" and float(r["exposure_ms"]) == exp]
        worst_sat = max(float(r["sat_pct"]) for r in exp_rows)
        worst_near = max(float(r["near_sat_pct"]) for r in exp_rows)
        worst_max = max(float(r["max"]) for r in exp_rows)
        state = "BAD" if worst_sat > 0.1 or worst_near > 1.0 else "OK"
        print(f"  {exp:g} ms: {state}, max={worst_max:.0f}, sat={worst_sat:.3f}%, near_sat={worst_near:.3f}%")

    print("Rule: for calibration, keep ROI sat_pct <= 0.1% and near_sat_pct <= 1%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

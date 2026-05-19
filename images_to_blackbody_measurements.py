#!/usr/bin/env python3
"""
Build a blackbody measurement CSV from camera calibration images.

Expected light image names:
    <temperature_c>_<exposure_ms>_<repeat>.<raw_ext>

Examples:
    900_100_1.raw
    900_100_2.raw
    1000_50_1.raw

Optional dark image names in --dark-dir:
    dark_<exposure_ms>_<repeat>.<raw_ext>
    <exposure_ms>_<repeat>.<raw_ext>

The script averages ROI RGB values over repeated captures and writes a CSV
compatible with radiometric_calibration_rgb.py blackbody mode.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


LIGHT_NAME_RE = re.compile(
    r"^(?P<temp>-?\d+(?:\.\d+)?)_(?P<exposure>\d+(?:\.\d+)?)_(?P<repeat>\d+)$"
)
DARK_NAME_RE = re.compile(
    r"^(?:dark_)?(?P<exposure>\d+(?:\.\d+)?)_(?P<repeat>\d+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    temperature_c: float | None
    exposure_ms: float
    repeat: int


def parse_roi(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height")
    try:
        x, y, width, height = [int(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI values must be integers") from exc
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("ROI must have non-negative x/y and positive width/height")
    return x, y, width, height


def parse_channel_order(text: str) -> tuple[int, int, int]:
    order = text.strip().lower()
    if sorted(order) != ["b", "g", "r"]:
        raise argparse.ArgumentTypeError("channel order must contain r, g, and b once, for example rgb or bgr")
    return tuple(order.index(ch) for ch in "rgb")


def float_key(value: float) -> str:
    return f"{value:g}"


def iter_raw_files(folder: Path, raw_ext: str) -> Iterable[Path]:
    suffix = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
    yield from sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix.lower())


def collect_light_records(folder: Path, raw_ext: str) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    ignored: list[str] = []
    for path in iter_raw_files(folder, raw_ext):
        match = LIGHT_NAME_RE.match(path.stem)
        if not match:
            ignored.append(path.name)
            continue
        records.append(
            ImageRecord(
                path=path,
                temperature_c=float(match.group("temp")),
                exposure_ms=float(match.group("exposure")),
                repeat=int(match.group("repeat")),
            )
        )
    if ignored:
        print(f"Ignored {len(ignored)} light files whose names do not match temp_exposure_repeat.")
    if not records:
        raise ValueError(f"no usable light raw files found in {folder}")
    return records


def collect_dark_records(folder: Path, raw_ext: str) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    ignored: list[str] = []
    for path in iter_raw_files(folder, raw_ext):
        match = DARK_NAME_RE.match(path.stem)
        if not match:
            ignored.append(path.name)
            continue
        records.append(
            ImageRecord(
                path=path,
                temperature_c=None,
                exposure_ms=float(match.group("exposure")),
                repeat=int(match.group("repeat")),
            )
        )
    if ignored:
        print(f"Ignored {len(ignored)} dark files whose names do not match dark_exposure_repeat.")
    if not records:
        raise ValueError(f"no usable dark raw files found in {folder}")
    return records


def read_raw_rgb(
    path: Path,
    width: int,
    height: int,
    dtype: str,
    channels: int,
    channel_order: tuple[int, int, int],
    byte_order: str,
) -> np.ndarray:
    np_dtype = np.dtype(dtype)
    if byte_order != "native":
        np_dtype = np_dtype.newbyteorder("<" if byte_order == "little" else ">")

    data = np.fromfile(path, dtype=np_dtype)
    expected = width * height * channels
    if data.size != expected:
        raise ValueError(
            f"{path.name}: expected {expected} values for {width}x{height}x{channels}, got {data.size}"
        )

    image = data.reshape((height, width, channels))
    if channels < 3:
        raise ValueError("this converter expects packed RGB/BGR-like raw data with at least 3 channels")
    return image[:, :, list(channel_order)].astype(np.float64, copy=False)


def roi_mean_rgb(image: np.ndarray, roi: tuple[int, int, int, int], path: Path) -> tuple[float, float, float]:
    x, y, width, height = roi
    y2 = y + height
    x2 = x + width
    if y2 > image.shape[0] or x2 > image.shape[1]:
        raise ValueError(f"{path.name}: ROI {roi} exceeds image size {image.shape[1]}x{image.shape[0]}")
    pixels = image[y:y2, x:x2, :3]
    mean = pixels.reshape((-1, 3)).mean(axis=0)
    return float(mean[0]), float(mean[1]), float(mean[2])


def average_records(
    records: Iterable[ImageRecord],
    args: argparse.Namespace,
) -> dict[tuple[float | None, float], tuple[float, float, float, int]]:
    grouped: dict[tuple[float | None, float], list[tuple[float, float, float]]] = defaultdict(list)
    for record in records:
        image = read_raw_rgb(
            record.path,
            args.width,
            args.height,
            args.dtype,
            args.channels,
            args.channel_order,
            args.byte_order,
        )
        grouped[(record.temperature_c, record.exposure_ms)].append(roi_mean_rgb(image, args.roi, record.path))

    averaged: dict[tuple[float | None, float], tuple[float, float, float, int]] = {}
    for key, values in grouped.items():
        arr = np.asarray(values, dtype=np.float64)
        mean = arr.mean(axis=0)
        averaged[key] = (float(mean[0]), float(mean[1]), float(mean[2]), len(values))
    return averaged


def dark_values_for_exposure(
    exposure_ms: float,
    dark_by_exposure: dict[float, tuple[float, float, float, int]],
    fallback: tuple[float, float, float] | None,
) -> tuple[float, float, float]:
    if exposure_ms in dark_by_exposure:
        r, g, b, _ = dark_by_exposure[exposure_ms]
        return r, g, b
    if fallback is not None:
        return fallback
    available = ", ".join(float_key(v) for v in sorted(dark_by_exposure))
    raise ValueError(f"missing dark frame for exposure {float_key(exposure_ms)} ms; available: {available}")


def write_csv(
    output_csv: Path,
    light_averages: dict[tuple[float | None, float], tuple[float, float, float, int]],
    dark_averages: dict[float, tuple[float, float, float, int]],
    dark_fallback: tuple[float, float, float] | None,
    emissivity: float,
) -> None:
    rows = build_measurement_rows(light_averages, dark_averages, dark_fallback, emissivity)
    write_measurement_rows_csv(rows, output_csv)


def write_measurement_rows_csv(rows: list[dict[str, object]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "level",
                "blackbody_temp_c",
                "emissivity",
                "exposure_ms",
                "dn_r",
                "dn_g",
                "dn_b",
                "dark_r",
                "dark_g",
                "dark_b",
                "repeat_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows: {output_csv}")


def build_measurement_rows(
    light_averages: dict[tuple[float | None, float], tuple[float, float, float, int]],
    dark_averages: dict[float, tuple[float, float, float, int]],
    dark_fallback: tuple[float, float, float] | None,
    emissivity: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (temperature_c, exposure_ms), (dn_r, dn_g, dn_b, light_count) in sorted(
        light_averages.items(), key=lambda item: (float(item[0][0] or 0), item[0][1])
    ):
        if temperature_c is None:
            continue
        dark_r, dark_g, dark_b = dark_values_for_exposure(exposure_ms, dark_averages, dark_fallback)
        rows.append(
            {
                "level": f"T{float_key(temperature_c)}",
                "blackbody_temp_c": float_key(temperature_c),
                "emissivity": float_key(emissivity),
                "exposure_ms": float_key(exposure_ms),
                "dn_r": f"{dn_r:.6f}",
                "dn_g": f"{dn_g:.6f}",
                "dn_b": f"{dn_b:.6f}",
                "dark_r": f"{dark_r:.6f}",
                "dark_g": f"{dark_g:.6f}",
                "dark_b": f"{dark_b:.6f}",
                "repeat_count": light_count,
            }
        )
    return rows


def build_rows_from_image_folder(args: argparse.Namespace) -> list[dict[str, object]]:
    light_records = collect_light_records(args.image_dir, args.raw_ext)
    light_averages = average_records(light_records, args)

    dark_averages: dict[float, tuple[float, float, float, int]] = {}
    dark_fallback: tuple[float, float, float] | None = None
    if args.dark_dir is not None:
        dark_records = collect_dark_records(args.dark_dir, args.raw_ext)
        dark_grouped = average_records(dark_records, args)
        dark_averages = {exposure: values for (_, exposure), values in dark_grouped.items()}
    elif args.use_zero_dark:
        dark_fallback = (0.0, 0.0, 0.0)
    else:
        dark_fallback = (float(args.dark_r), float(args.dark_g), float(args.dark_b))

    return build_measurement_rows(light_averages, dark_averages, dark_fallback, args.emissivity)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert blackbody calibration raw image folders into a measurement CSV."
    )
    parser.add_argument("--image-dir", type=Path, required=True, help="folder containing light raw images")
    parser.add_argument("--output-csv", type=Path, required=True, help="CSV to write")
    parser.add_argument("--roi", type=parse_roi, required=True, help="ROI as x,y,width,height, using BMP pixel coordinates")
    parser.add_argument("--width", type=int, required=True, help="raw image width in pixels")
    parser.add_argument("--height", type=int, required=True, help="raw image height in pixels")
    parser.add_argument("--dtype", default="uint16", help="raw sample dtype, for example uint8, uint16, >u2, <u2")
    parser.add_argument("--channels", type=int, default=3, help="number of packed channels per pixel")
    parser.add_argument("--channel-order", type=parse_channel_order, default=parse_channel_order("rgb"), help="raw channel order, rgb or bgr")
    parser.add_argument("--byte-order", choices=["native", "little", "big"], default="native")
    parser.add_argument("--raw-ext", default=".raw", help="raw file extension, default .raw")
    parser.add_argument("--emissivity", type=float, default=1.0, help="blackbody emissivity")
    parser.add_argument("--dark-dir", type=Path, help="optional folder containing dark raw images")
    parser.add_argument("--dark-r", type=float, help="constant dark R value")
    parser.add_argument("--dark-g", type=float, help="constant dark G value")
    parser.add_argument("--dark-b", type=float, help="constant dark B value")
    parser.add_argument("--use-zero-dark", action="store_true", help="write dark values as 0 when no dark frames are available")
    args = parser.parse_args()

    if args.width <= 0 or args.height <= 0 or args.channels < 3:
        parser.error("--width/--height must be positive and --channels must be at least 3")

    constant_dark_values = [args.dark_r, args.dark_g, args.dark_b]
    has_partial_constant_dark = any(v is not None for v in constant_dark_values) and not all(
        v is not None for v in constant_dark_values
    )
    if has_partial_constant_dark:
        parser.error("--dark-r, --dark-g, and --dark-b must be provided together")

    dark_source_count = int(args.dark_dir is not None) + int(all(v is not None for v in constant_dark_values)) + int(
        args.use_zero_dark
    )
    if dark_source_count == 0:
        parser.error("provide --dark-dir, constant --dark-r/--dark-g/--dark-b, or --use-zero-dark")
    if dark_source_count > 1:
        parser.error("choose only one dark source: --dark-dir, constant dark values, or --use-zero-dark")

    return args


def main() -> None:
    args = parse_args()

    rows = build_rows_from_image_folder(args)
    write_measurement_rows_csv(rows, args.output_csv)


if __name__ == "__main__":
    main()

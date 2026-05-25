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
from typing import Iterable, Literal

import numpy as np


LIGHT_NAME_RE = re.compile(
    r"^(?P<temp>-?\d+(?:\.\d+)?)_(?P<exposure>\d+(?:\.\d+)?)_(?P<repeat>\d+)$"
)
DARK_NAME_RE = re.compile(
    r"^(?:dark_)?(?P<exposure>\d+(?:\.\d+)?)_(?P<repeat>\d+)$",
    re.IGNORECASE,
)
EXPOSURE_MATCH_TOLERANCE_MS = 0.002
TEMPERATURE_MATCH_TOLERANCE_C = 1e-6


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    temperature_c: float | None
    exposure_ms: float
    repeat: int


@dataclass(frozen=True)
class AveragedRecord:
    dn_r: float
    dn_g: float
    dn_b: float
    repeat_count: int
    repeat_ids: tuple[int, ...]
    missing_repeats: tuple[int, ...]


@dataclass(frozen=True)
class RoiResult:
    roi: tuple[int, int, int, int]
    method: str
    confidence: float
    center_x: float
    center_y: float
    anchor_x: float | None
    anchor_y: float | None
    message: str


@dataclass(frozen=True)
class BadPixelMasks:
    masks_by_exposure: dict[float, np.ndarray]
    audit_rows: list[dict[str, object]]

    def mask_for_exposure(self, exposure_ms: float) -> np.ndarray | None:
        if exposure_ms in self.masks_by_exposure:
            return self.masks_by_exposure[exposure_ms]
        close_matches = [
            (abs(exposure_ms - available_exposure), available_exposure)
            for available_exposure in self.masks_by_exposure
            if abs(exposure_ms - available_exposure) <= EXPOSURE_MATCH_TOLERANCE_MS
        ]
        if close_matches:
            _, matched_exposure = min(close_matches)
            return self.masks_by_exposure[matched_exposure]
        return None


class RoiSelector:
    def __init__(self, args: argparse.Namespace, records: Iterable[ImageRecord]):
        self.args = args
        self.records = list(records)
        self.anchor: RoiResult | None = None
        self.audit_rows: list[dict[str, object]] = []
        if args.roi_mode == "manual":
            if args.roi is None:
                raise ValueError("--roi is required when --roi-mode manual")
            self.manual_roi = args.roi
        else:
            self.manual_roi = None
            self.anchor = self._build_anchor()

    def roi_for(self, record: ImageRecord, frame_type: Literal["light", "dark"]) -> tuple[int, int, int, int]:
        if self.args.roi_mode == "manual":
            roi = self.manual_roi
            assert roi is not None
            result = RoiResult(roi, "manual", 1.0, roi[0] + roi[2] / 2.0, roi[1] + roi[3] / 2.0, None, None, "")
        elif frame_type == "dark":
            result = self._anchor_fallback("dark uses light anchor ROI")
        else:
            result = self._detect_record_roi(record)

        shift_px = ""
        if result.anchor_x is not None and result.anchor_y is not None:
            shift_px = f"{np.hypot(result.center_x - result.anchor_x, result.center_y - result.anchor_y):.3f}"
        self.audit_rows.append(
            {
                "frame_type": frame_type,
                "file": str(record.path),
                "blackbody_temp_c": "" if record.temperature_c is None else float_key(record.temperature_c),
                "exposure_ms": float_key(record.exposure_ms),
                "repeat": record.repeat,
                "roi_x": result.roi[0],
                "roi_y": result.roi[1],
                "roi_width": result.roi[2],
                "roi_height": result.roi[3],
                "roi_method": result.method,
                "confidence": f"{result.confidence:.6f}",
                "shift_px": shift_px,
                "center_x": f"{result.center_x:.3f}",
                "center_y": f"{result.center_y:.3f}",
                "anchor_x": "" if result.anchor_x is None else f"{result.anchor_x:.3f}",
                "anchor_y": "" if result.anchor_y is None else f"{result.anchor_y:.3f}",
                "message": result.message,
            }
        )
        return result.roi

    def write_audit_csv(self, output_csv: Path) -> None:
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "frame_type",
                    "file",
                    "blackbody_temp_c",
                    "exposure_ms",
                    "repeat",
                    "roi_x",
                    "roi_y",
                    "roi_width",
                    "roi_height",
                    "roi_method",
                    "confidence",
                    "shift_px",
                    "center_x",
                    "center_y",
                    "anchor_x",
                    "anchor_y",
                    "message",
                ],
            )
            writer.writeheader()
            writer.writerows(self.audit_rows)
        print(f"Wrote ROI audit: {output_csv}")

    def _build_anchor(self) -> RoiResult:
        candidates = [
            record
            for record in self.records
            if record.temperature_c is not None and record.temperature_c in self.args.anchor_temperatures
        ]
        if not candidates:
            max_temp = max(record.temperature_c for record in self.records if record.temperature_c is not None)
            candidates = [record for record in self.records if record.temperature_c == max_temp]

        detections: list[RoiResult] = []
        for record in candidates:
            result = detect_hotspot(record.path.with_suffix(".bmp"), self.args.roi_size, None, self.args.search_radius)
            if result is not None:
                detections.append(result)

        usable = [result for result in detections if result.confidence >= self.args.anchor_min_confidence]
        if not usable:
            raise ValueError(
                "auto-anchor ROI failed: no reliable hotspot found in anchor-temperature BMP files. "
                "Use --roi-mode manual --roi x,y,w,h or lower --anchor-min-confidence."
            )

        center_x = float(np.median([result.center_x for result in usable]))
        center_y = float(np.median([result.center_y for result in usable]))
        roi = centered_roi(center_x, center_y, self.args.roi_size, self.args.width, self.args.height)
        return RoiResult(roi, "anchor_detected", float(np.median([r.confidence for r in usable])), center_x, center_y, None, None, "")

    def _detect_record_roi(self, record: ImageRecord) -> RoiResult:
        assert self.anchor is not None
        result = detect_hotspot(
            record.path.with_suffix(".bmp"),
            self.args.roi_size,
            (self.anchor.center_x, self.anchor.center_y),
            self.args.search_radius,
        )
        if result is None:
            return self._anchor_fallback("local hotspot detection failed")

        shift = float(np.hypot(result.center_x - self.anchor.center_x, result.center_y - self.anchor.center_y))
        if shift > self.args.max_roi_shift:
            return self._anchor_fallback(f"local hotspot shifted {shift:.1f}px from anchor")
        if result.confidence < self.args.local_min_confidence:
            return self._anchor_fallback(f"local hotspot confidence {result.confidence:.3f} below threshold")

        return RoiResult(
            result.roi,
            "local_refined",
            result.confidence,
            result.center_x,
            result.center_y,
            self.anchor.center_x,
            self.anchor.center_y,
            "",
        )

    def _anchor_fallback(self, message: str) -> RoiResult:
        assert self.anchor is not None
        return RoiResult(
            self.anchor.roi,
            "anchor_fallback",
            self.anchor.confidence,
            self.anchor.center_x,
            self.anchor.center_y,
            self.anchor.center_x,
            self.anchor.center_y,
            message,
        )


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


def parse_bayer_pattern(text: str) -> str:
    pattern = text.strip().lower()
    if pattern not in {"rggb", "bggr", "grbg", "gbrg"}:
        raise argparse.ArgumentTypeError("Bayer pattern must be one of rggb, bggr, grbg, gbrg")
    return pattern


def parse_repeat_list(text: str) -> tuple[int, ...]:
    try:
        repeats = tuple(sorted({int(part.strip()) for part in text.split(",") if part.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("repeats must be comma-separated integers, for example 1,2,3") from exc
    if not repeats:
        raise argparse.ArgumentTypeError("repeat list cannot be empty")
    if repeats[0] <= 0:
        raise argparse.ArgumentTypeError("repeat numbers must be positive")
    return repeats


def parse_float_list(text: str) -> tuple[float, ...]:
    try:
        values = tuple(sorted({float(part.strip()) for part in text.split(",") if part.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("values must be comma-separated numbers, for example 1400,1500") from exc
    if not values:
        raise argparse.ArgumentTypeError("list cannot be empty")
    return values


def float_key(value: float) -> str:
    return f"{value:g}"


def repeat_counts(repeats: Iterable[int]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for repeat in repeats:
        counts[repeat] += 1
    return counts


def format_repeat_list(repeats: Iterable[int]) -> str:
    values = list(repeats)
    return ",".join(str(repeat) for repeat in values) if values else "none"


def group_label(key: tuple[float | None, float]) -> str:
    temperature_c, exposure_ms = key
    if temperature_c is None:
        return f"dark exposure {float_key(exposure_ms)} ms"
    return f"T{float_key(temperature_c)} exposure {float_key(exposure_ms)} ms"


def load_included_conditions(path: Path) -> set[tuple[float, float]]:
    conditions: set[tuple[float, float]] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"condition CSV has no header: {path}")
        missing = {"temperature_c", "exposure_ms"} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"condition CSV missing column(s): {', '.join(sorted(missing))}")

        for row in reader:
            planned = str(row.get("planned", "Y")).strip().upper()
            if planned not in {"Y", "YES", "TRUE", "1"}:
                continue
            conditions.add((float(row["temperature_c"]), float(row["exposure_ms"])))
    if not conditions:
        raise ValueError(f"condition CSV has no included rows: {path}")
    return conditions


def condition_matches(record: ImageRecord, allowed: set[tuple[float, float]]) -> bool:
    if record.temperature_c is None:
        return False
    return any(
        abs(record.temperature_c - temperature_c) <= TEMPERATURE_MATCH_TOLERANCE_C
        and abs(record.exposure_ms - exposure_ms) <= EXPOSURE_MATCH_TOLERANCE_MS
        for temperature_c, exposure_ms in allowed
    )


def centered_roi(center_x: float, center_y: float, size: int, image_width: int, image_height: int) -> tuple[int, int, int, int]:
    half = size / 2.0
    x = int(round(center_x - half))
    y = int(round(center_y - half))
    x = min(max(x, 0), image_width - size)
    y = min(max(y, 0), image_height - size)
    return x, y, size, size


def read_bmp_grayscale(path: Path) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"BMP preview not found for auto ROI: {path}")
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("auto ROI requires Pillow; install pillow or use --roi-mode manual") from exc
    return np.asarray(Image.open(path).convert("L"), dtype=np.float64)


def detect_hotspot(
    bmp_path: Path,
    roi_size: int,
    anchor: tuple[float, float] | None,
    search_radius: int,
) -> RoiResult | None:
    try:
        image = read_bmp_grayscale(bmp_path)
    except FileNotFoundError:
        return None

    image_height, image_width = image.shape
    if anchor is None:
        x0, y0, x1, y1 = 0, 0, image_width, image_height
    else:
        ax, ay = anchor
        x0 = max(0, int(round(ax - search_radius)))
        y0 = max(0, int(round(ay - search_radius)))
        x1 = min(image_width, int(round(ax + search_radius)))
        y1 = min(image_height, int(round(ay + search_radius)))
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return None

    lo, hi = float(np.percentile(crop, 50.0)), float(np.percentile(crop, 99.7))
    if hi <= lo:
        return None
    threshold = lo + 0.70 * (hi - lo)
    mask = crop >= threshold

    component = largest_component(mask)
    if component is None:
        return None
    ys, xs = component
    area = int(xs.size)
    if area < max(25, roi_size * roi_size // 8):
        return None

    center_x = float(xs.mean() + x0)
    center_y = float(ys.mean() + y0)
    bbox_w = int(xs.max() - xs.min() + 1)
    bbox_h = int(ys.max() - ys.min() + 1)
    aspect = min(bbox_w, bbox_h) / max(bbox_w, bbox_h)
    fill = area / max(1, bbox_w * bbox_h)
    expected_area = np.pi * (max(roi_size, min(bbox_w, bbox_h)) / 2.0) ** 2
    area_score = min(1.0, area / max(1.0, expected_area))
    confidence = float(max(0.0, min(1.0, 0.45 * aspect + 0.35 * fill + 0.20 * area_score)))
    roi = centered_roi(center_x, center_y, roi_size, image_width, image_height)
    return RoiResult(roi, "detected", confidence, center_x, center_y, anchor[0] if anchor else None, anchor[1] if anchor else None, "")


def largest_component(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    ys, xs = np.where(mask)
    if ys.size == 0:
        return None

    remaining = set(zip(ys.tolist(), xs.tolist()))
    best: list[tuple[int, int]] = []
    height, width = mask.shape
    while remaining:
        start = remaining.pop()
        stack = [start]
        comp = [start]
        while stack:
            y, x = stack.pop()
            for ny in (y - 1, y, y + 1):
                for nx in (x - 1, x, x + 1):
                    if ny == y and nx == x:
                        continue
                    if ny < 0 or nx < 0 or ny >= height or nx >= width:
                        continue
                    point = (ny, nx)
                    if point in remaining:
                        remaining.remove(point)
                        stack.append(point)
                        comp.append(point)
        if len(comp) > len(best):
            best = comp

    if not best:
        return None
    comp_arr = np.asarray(best, dtype=np.int32)
    return comp_arr[:, 0], comp_arr[:, 1]


def iter_image_files(folder: Path, image_ext: str) -> Iterable[Path]:
    suffix = image_ext if image_ext.startswith(".") else f".{image_ext}"
    yield from sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix.lower())


def collect_light_records(folder: Path, image_ext: str) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    ignored: list[str] = []
    for path in iter_image_files(folder, image_ext):
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
        raise ValueError(f"no usable light image files found in {folder}")
    return records


def collect_dark_records(folder: Path, image_ext: str) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    ignored: list[str] = []
    for path in iter_image_files(folder, image_ext):
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
        raise ValueError(f"no usable dark image files found in {folder}")
    return records


def read_bmp_rgb(path: Path) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"BMP image not found: {path}")
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("BMP input requires Pillow") from exc
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)


def infer_bmp_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        raise FileNotFoundError(f"BMP image not found: {path}")
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("BMP input requires Pillow") from exc
    with Image.open(path) as image:
        return image.size


def read_raw_image(
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

    if channels == 1:
        return data.reshape((height, width)).astype(np.float64, copy=False)

    image = data.reshape((height, width, channels))
    if channels < 3:
        raise ValueError("packed color raw data must have at least 3 channels")
    return image[:, :, list(channel_order)].astype(np.float64, copy=False)


def read_measurement_image(path: Path, args: argparse.Namespace) -> np.ndarray:
    if args.input_format == "bmp":
        return read_bmp_rgb(path)
    return read_raw_image(
        path,
        args.width,
        args.height,
        args.dtype,
        args.channels,
        args.channel_order,
        args.byte_order,
    )


def build_bad_pixel_masks(records: Iterable[ImageRecord], args: argparse.Namespace) -> BadPixelMasks:
    grouped: dict[float, list[ImageRecord]] = defaultdict(list)
    for record in records:
        grouped[record.exposure_ms].append(record)

    masks_by_exposure: dict[float, np.ndarray] = {}
    audit_rows: list[dict[str, object]] = []
    total_pixels = args.width * args.height
    for exposure_ms, exposure_records in sorted(grouped.items()):
        images = [
            read_measurement_image(record.path, args)
            for record in exposure_records
        ]
        if args.input_format == "raw" and args.raw_format == "bayer":
            dark_stat = np.median(np.stack(images, axis=0), axis=0)
            mask = dark_stat >= args.bad_pixel_threshold
        else:
            dark_stat = np.median(np.stack(images, axis=0), axis=0)
            mask = np.any(dark_stat[:, :, :3] >= args.bad_pixel_threshold, axis=2)
        masks_by_exposure[exposure_ms] = mask
        count = int(np.count_nonzero(mask))
        audit_rows.append(
            {
                "exposure_ms": float_key(exposure_ms),
                "dark_repeat_count": len(exposure_records),
                "bad_pixel_threshold": args.bad_pixel_threshold,
                "bad_pixel_count": count,
                "bad_pixel_percent": f"{count / total_pixels * 100.0:.8f}",
            }
        )
    return BadPixelMasks(masks_by_exposure, audit_rows)


def write_bad_pixel_audit_csv(output_csv: Path, masks: BadPixelMasks) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "exposure_ms",
                "dark_repeat_count",
                "bad_pixel_threshold",
                "bad_pixel_count",
                "bad_pixel_percent",
            ],
        )
        writer.writeheader()
        writer.writerows(masks.audit_rows)
    print(f"Wrote bad pixel audit: {output_csv}")


def roi_mean_packed_rgb(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    path: Path,
    bad_mask: np.ndarray | None = None,
) -> tuple[float, float, float]:
    x, y, width, height = roi
    y2 = y + height
    x2 = x + width
    if y2 > image.shape[0] or x2 > image.shape[1]:
        raise ValueError(f"{path.name}: ROI {roi} exceeds image size {image.shape[1]}x{image.shape[0]}")
    pixels = image[y:y2, x:x2, :3]
    if bad_mask is not None:
        good = ~bad_mask[y:y2, x:x2]
        if not np.any(good):
            raise ValueError(f"{path.name}: all ROI pixels are masked as bad")
        mean = pixels[good].reshape((-1, 3)).mean(axis=0)
    else:
        mean = pixels.reshape((-1, 3)).mean(axis=0)
    return float(mean[0]), float(mean[1]), float(mean[2])


def roi_mean_bayer(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    pattern: str,
    path: Path,
    bad_mask: np.ndarray | None = None,
) -> tuple[float, float, float]:
    x, y, width, height = roi
    y2 = y + height
    x2 = x + width
    if y2 > image.shape[0] or x2 > image.shape[1]:
        raise ValueError(f"{path.name}: ROI {roi} exceeds image size {image.shape[1]}x{image.shape[0]}")

    roi_image = image[y:y2, x:x2]
    roi_bad_mask = bad_mask[y:y2, x:x2] if bad_mask is not None else None
    color_at = {
        (0, 0): pattern[0],
        (0, 1): pattern[1],
        (1, 0): pattern[2],
        (1, 1): pattern[3],
    }
    means: dict[str, list[float]] = {"r": [], "g": [], "b": []}
    for row_parity in (0, 1):
        for col_parity in (0, 1):
            color = color_at[((y + row_parity) % 2, (x + col_parity) % 2)]
            samples = roi_image[row_parity::2, col_parity::2]
            if roi_bad_mask is not None:
                sample_mask = roi_bad_mask[row_parity::2, col_parity::2]
                samples = samples[~sample_mask]
            if samples.size:
                means[color].append(float(samples.mean()))

    missing = [color for color, values in means.items() if not values]
    if missing:
        raise ValueError(f"{path.name}: ROI is too small to include Bayer colors: {missing}")
    return (
        float(np.mean(means["r"])),
        float(np.mean(means["g"])),
        float(np.mean(means["b"])),
    )


def roi_mean_rgb(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    args: argparse.Namespace,
    path: Path,
    bad_mask: np.ndarray | None = None,
) -> tuple[float, float, float]:
    if args.raw_format == "bayer":
        if image.ndim != 2:
            raise ValueError("--raw-format bayer expects --channels 1")
        return roi_mean_bayer(image, roi, args.bayer_pattern, path, bad_mask)
    if image.ndim != 3:
        raise ValueError("--raw-format rgb expects packed RGB/BGR data")
    return roi_mean_packed_rgb(image, roi, path, bad_mask)


def average_records(
    records: Iterable[ImageRecord],
    args: argparse.Namespace,
    roi_selector: RoiSelector,
    frame_type: Literal["light", "dark"],
    bad_pixel_masks: BadPixelMasks | None = None,
) -> dict[tuple[float | None, float], AveragedRecord]:
    grouped: dict[tuple[float | None, float], list[tuple[int, tuple[float, float, float]]]] = defaultdict(list)
    for record in records:
        image = read_measurement_image(record.path, args)
        roi = roi_selector.roi_for(record, frame_type)
        bad_mask = bad_pixel_masks.mask_for_exposure(record.exposure_ms) if bad_pixel_masks is not None else None
        grouped[(record.temperature_c, record.exposure_ms)].append(
            (record.repeat, roi_mean_rgb(image, roi, args, record.path, bad_mask))
        )

    averaged: dict[tuple[float | None, float], AveragedRecord] = {}
    for key, repeat_values in grouped.items():
        repeats = tuple(sorted(repeat for repeat, _ in repeat_values))
        missing_repeats = tuple(repeat for repeat in args.expected_repeats if repeat not in repeats)
        duplicate_repeats = sorted(repeat for repeat, count in repeat_counts(repeats).items() if count > 1)
        label = group_label(key)

        if duplicate_repeats:
            print(f"WARNING: {label} has duplicate repeat(s) {format_repeat_list(duplicate_repeats)}; all files are included.")
        if missing_repeats and args.missing_repeat_policy != "ignore":
            message = (
                f"{label} missing repeat(s) {format_repeat_list(missing_repeats)}; "
                f"averaging available repeat(s) {format_repeat_list(repeats)}."
            )
            if args.missing_repeat_policy == "error":
                raise ValueError(message)
            print(f"WARNING: {message}")
        if len(repeat_values) < args.min_repeats:
            raise ValueError(
                f"{label} has only {len(repeat_values)} repeat(s), below --min-repeats {args.min_repeats}"
            )

        values = [value for _, value in repeat_values]
        arr = np.asarray(values, dtype=np.float64)
        mean = arr.mean(axis=0)
        averaged[key] = AveragedRecord(
            dn_r=float(mean[0]),
            dn_g=float(mean[1]),
            dn_b=float(mean[2]),
            repeat_count=len(repeat_values),
            repeat_ids=repeats,
            missing_repeats=missing_repeats,
        )
    return averaged


def dark_values_for_exposure(
    exposure_ms: float,
    dark_by_exposure: dict[float, AveragedRecord],
    fallback: tuple[float, float, float] | None,
) -> tuple[float, float, float]:
    if exposure_ms in dark_by_exposure:
        average = dark_by_exposure[exposure_ms]
        return average.dn_r, average.dn_g, average.dn_b
    close_matches = [
        (abs(exposure_ms - available_exposure), available_exposure)
        for available_exposure in dark_by_exposure
        if abs(exposure_ms - available_exposure) <= EXPOSURE_MATCH_TOLERANCE_MS
    ]
    if close_matches:
        _, matched_exposure = min(close_matches)
        average = dark_by_exposure[matched_exposure]
        return average.dn_r, average.dn_g, average.dn_b
    if fallback is not None:
        return fallback
    available = ", ".join(float_key(v) for v in sorted(dark_by_exposure))
    raise ValueError(f"missing dark frame for exposure {float_key(exposure_ms)} ms; available: {available}")


def write_csv(
    output_csv: Path,
    light_averages: dict[tuple[float | None, float], AveragedRecord],
    dark_averages: dict[float, AveragedRecord],
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
                "repeat_ids",
                "missing_repeats",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows: {output_csv}")


def build_measurement_rows(
    light_averages: dict[tuple[float | None, float], AveragedRecord],
    dark_averages: dict[float, AveragedRecord],
    dark_fallback: tuple[float, float, float] | None,
    emissivity: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (temperature_c, exposure_ms), average in sorted(
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
                "dn_r": f"{average.dn_r:.6f}",
                "dn_g": f"{average.dn_g:.6f}",
                "dn_b": f"{average.dn_b:.6f}",
                "dark_r": f"{dark_r:.6f}",
                "dark_g": f"{dark_g:.6f}",
                "dark_b": f"{dark_b:.6f}",
                "repeat_count": average.repeat_count,
                "repeat_ids": format_repeat_list(average.repeat_ids),
                "missing_repeats": format_repeat_list(average.missing_repeats),
            }
        )
    return rows


def write_repeat_audit_csv(
    output_csv: Path,
    light_averages: dict[tuple[float | None, float], AveragedRecord],
    dark_averages: dict[float, AveragedRecord],
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "frame_type",
                "blackbody_temp_c",
                "exposure_ms",
                "repeat_count",
                "repeat_ids",
                "missing_repeats",
            ],
        )
        writer.writeheader()
        for (temperature_c, exposure_ms), average in sorted(
            light_averages.items(), key=lambda item: (float(item[0][0] or 0), item[0][1])
        ):
            if temperature_c is None:
                continue
            writer.writerow(
                {
                    "frame_type": "light",
                    "blackbody_temp_c": float_key(temperature_c),
                    "exposure_ms": float_key(exposure_ms),
                    "repeat_count": average.repeat_count,
                    "repeat_ids": format_repeat_list(average.repeat_ids),
                    "missing_repeats": format_repeat_list(average.missing_repeats),
                }
            )
        for exposure_ms, average in sorted(dark_averages.items()):
            writer.writerow(
                {
                    "frame_type": "dark",
                    "blackbody_temp_c": "",
                    "exposure_ms": float_key(exposure_ms),
                    "repeat_count": average.repeat_count,
                    "repeat_ids": format_repeat_list(average.repeat_ids),
                    "missing_repeats": format_repeat_list(average.missing_repeats),
                }
            )
    print(f"Wrote repeat audit: {output_csv}")


def build_rows_from_image_folder(args: argparse.Namespace) -> list[dict[str, object]]:
    image_ext = ".bmp" if args.input_format == "bmp" else args.raw_ext
    light_records = collect_light_records(args.image_dir, image_ext)
    if args.include_conditions_csv is not None:
        allowed_conditions = load_included_conditions(args.include_conditions_csv)
        before_count = len(light_records)
        light_records = [record for record in light_records if condition_matches(record, allowed_conditions)]
        print(f"Included {len(light_records)} of {before_count} light image files from condition CSV: {args.include_conditions_csv}")
        if not light_records:
            raise ValueError("no light image files matched --include-conditions-csv")
    if args.input_format == "bmp" and (args.width is None or args.height is None):
        args.width, args.height = infer_bmp_size(light_records[0].path)
        print(f"Inferred BMP size: {args.width}x{args.height}")
    roi_selector = RoiSelector(args, light_records)

    dark_averages: dict[float, AveragedRecord] = {}
    dark_fallback: tuple[float, float, float] | None = None
    dark_records: list[ImageRecord] = []
    if args.dark_dir is not None:
        dark_records = collect_dark_records(args.dark_dir, image_ext)
    elif args.use_zero_dark:
        dark_fallback = (0.0, 0.0, 0.0)
    else:
        dark_fallback = (float(args.dark_r), float(args.dark_g), float(args.dark_b))

    bad_pixel_masks: BadPixelMasks | None = None
    if args.bad_pixel_policy != "none":
        if not dark_records:
            raise ValueError("--bad-pixel-policy requires --dark-dir so masks can be built from dark frames")
        bad_pixel_masks = build_bad_pixel_masks(dark_records, args)
        total_bad = sum(int(row["bad_pixel_count"]) for row in bad_pixel_masks.audit_rows)
        print(f"Built bad-pixel masks from dark frames; total per-exposure bad-pixel count sum: {total_bad}")

    light_averages = average_records(light_records, args, roi_selector, "light", bad_pixel_masks)

    if dark_records:
        dark_grouped = average_records(dark_records, args, roi_selector, "dark", bad_pixel_masks)
        dark_averages = {exposure: values for (_, exposure), values in dark_grouped.items()}

    if args.repeat_audit_csv is not None:
        write_repeat_audit_csv(args.repeat_audit_csv, light_averages, dark_averages)
    if args.roi_audit_csv is not None:
        roi_selector.write_audit_csv(args.roi_audit_csv)
    if args.bad_pixel_audit_csv is not None and bad_pixel_masks is not None:
        write_bad_pixel_audit_csv(args.bad_pixel_audit_csv, bad_pixel_masks)

    return build_measurement_rows(light_averages, dark_averages, dark_fallback, args.emissivity)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert blackbody calibration raw image folders into a measurement CSV."
    )
    parser.add_argument("--image-dir", type=Path, required=True, help="folder containing light raw images")
    parser.add_argument("--output-csv", type=Path, required=True, help="CSV to write")
    parser.add_argument(
        "--input-format",
        choices=["raw", "bmp"],
        default="raw",
        help="image data source to average: raw or bmp; default raw",
    )
    parser.add_argument("--roi", type=parse_roi, help="ROI as x,y,width,height, using BMP pixel coordinates")
    parser.add_argument(
        "--roi-mode",
        choices=["manual", "auto-anchor"],
        default="manual",
        help="manual uses --roi; auto-anchor detects a stable hotspot anchor from high-temperature BMP previews",
    )
    parser.add_argument(
        "--anchor-temperatures",
        type=parse_float_list,
        default=parse_float_list("1400,1500"),
        help="comma-separated temperatures used to detect the auto ROI anchor, default 1400,1500",
    )
    parser.add_argument("--roi-size", type=int, default=120, help="square ROI size for --roi-mode auto-anchor")
    parser.add_argument("--search-radius", type=int, default=220, help="local auto ROI search radius around the anchor")
    parser.add_argument("--max-roi-shift", type=float, default=180.0, help="maximum local ROI center shift before anchor fallback")
    parser.add_argument("--anchor-min-confidence", type=float, default=0.45, help="minimum confidence for anchor detections")
    parser.add_argument("--local-min-confidence", type=float, default=0.35, help="minimum confidence for local ROI refinement")
    parser.add_argument("--roi-audit-csv", type=Path, help="optional CSV listing ROI used for every image")
    parser.add_argument("--width", type=int, help="image width in pixels; required for raw and inferred for bmp when omitted")
    parser.add_argument("--height", type=int, help="image height in pixels; required for raw and inferred for bmp when omitted")
    parser.add_argument("--dtype", default="uint16", help="raw sample dtype, for example uint8, uint16, >u2, <u2")
    parser.add_argument("--channels", type=int, default=3, help="number of packed channels per pixel")
    parser.add_argument("--channel-order", type=parse_channel_order, default=parse_channel_order("rgb"), help="raw channel order, rgb or bgr")
    parser.add_argument("--raw-format", choices=["rgb", "bayer"], default="rgb", help="raw color format")
    parser.add_argument("--bayer-pattern", type=parse_bayer_pattern, default="gbrg", help="Bayer pattern for --raw-format bayer")
    parser.add_argument("--byte-order", choices=["native", "little", "big"], default="native")
    parser.add_argument("--raw-ext", default=".raw", help="raw file extension, default .raw")
    parser.add_argument(
        "--include-conditions-csv",
        type=Path,
        help="optional CSV with temperature_c/exposure_ms rows to include; planned=N rows are skipped when present",
    )
    parser.add_argument(
        "--expected-repeats",
        type=parse_repeat_list,
        default=parse_repeat_list("1,2,3"),
        help="comma-separated expected repeat numbers; missing ones are reported, default 1,2,3",
    )
    parser.add_argument(
        "--min-repeats",
        type=int,
        default=2,
        help="minimum available repeats required per temperature/exposure group, default 2",
    )
    parser.add_argument(
        "--missing-repeat-policy",
        choices=["warn", "error", "ignore"],
        default="warn",
        help="what to do when expected repeats are missing; default warn",
    )
    parser.add_argument("--repeat-audit-csv", type=Path, help="optional CSV listing present and missing repeats")
    parser.add_argument("--emissivity", type=float, default=1.0, help="blackbody emissivity")
    parser.add_argument("--dark-dir", type=Path, help="optional folder containing dark raw images")
    parser.add_argument(
        "--bad-pixel-policy",
        choices=["none", "exclude"],
        default="none",
        help="exclude uses dark RAW frames to mask bad pixels before ROI averaging; default none",
    )
    parser.add_argument(
        "--bad-pixel-threshold",
        type=float,
        default=8192.0,
        help="dark RAW median threshold for marking bad pixels, default 8192",
    )
    parser.add_argument("--bad-pixel-audit-csv", type=Path, help="optional CSV listing bad-pixel counts by exposure")
    parser.add_argument("--dark-r", type=float, help="constant dark R value")
    parser.add_argument("--dark-g", type=float, help="constant dark G value")
    parser.add_argument("--dark-b", type=float, help="constant dark B value")
    parser.add_argument("--use-zero-dark", action="store_true", help="write dark values as 0 when no dark frames are available")
    args = parser.parse_args()

    if args.input_format == "raw" and (args.width is None or args.height is None):
        parser.error("--width and --height are required for raw input")
    if args.width is not None and args.width <= 0:
        parser.error("--width must be positive")
    if args.height is not None and args.height <= 0:
        parser.error("--height must be positive")
    if args.roi_mode == "manual" and args.roi is None:
        parser.error("--roi is required when --roi-mode manual")
    if args.roi_mode == "auto-anchor":
        if args.roi_size <= 0:
            parser.error("--roi-size must be positive")
        if args.width is not None and args.height is not None and args.roi_size > min(args.width, args.height):
            parser.error("--roi-size must fit within the image")
        if args.search_radius <= 0:
            parser.error("--search-radius must be positive")
        if args.max_roi_shift < 0:
            parser.error("--max-roi-shift must be non-negative")
    if args.input_format == "bmp" and args.raw_format == "bayer":
        parser.error("--raw-format bayer is only valid for raw input")
    if args.raw_format == "rgb" and args.channels < 3:
        parser.error("--channels must be at least 3 for RGB mode")
    if args.raw_format == "bayer" and args.channels != 1:
        parser.error("--raw-format bayer requires --channels 1")
    if args.min_repeats <= 0:
        parser.error("--min-repeats must be positive")
    if args.bad_pixel_threshold <= 0:
        parser.error("--bad-pixel-threshold must be positive")
    if args.bad_pixel_policy != "none" and args.dark_dir is None:
        parser.error("--bad-pixel-policy requires --dark-dir")

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

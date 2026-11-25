#!/usr/bin/env python3
"""Validate the generated country masks, prompts, and manifest."""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any


ISO_PATTERN = re.compile(r"^[A-Z]{3}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png_header(path: Path) -> tuple[int, int, int, int]:
    """Return width, height, bit depth, and color type from a PNG header."""
    with path.open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            raise ValueError("invalid PNG signature")
        length_bytes = handle.read(4)
        chunk_type = handle.read(4)
        if len(length_bytes) != 4 or chunk_type != b"IHDR":
            raise ValueError("missing IHDR chunk")
        length = struct.unpack(">I", length_bytes)[0]
        if length < 13:
            raise ValueError("invalid IHDR chunk")
        data = handle.read(length)
        if len(data) != length:
            raise ValueError("truncated IHDR chunk")
        width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        return width, height, bit_depth, color_type


def add_error(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_manifest(root: Path, errors: list[str]) -> list[dict[str, Any]]:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        add_error(errors, f"missing {manifest_path}")
        return []
    except json.JSONDecodeError as exc:
        add_error(errors, f"invalid JSON in {manifest_path}: {exc}")
        return []

    if not isinstance(manifest, list):
        add_error(errors, "manifest.json must contain an array")
        return []

    required_fields = {"iso", "name", "en", "coords", "outline"}
    isos: set[str] = set()
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            add_error(errors, f"manifest entry {index} must be an object")
            continue

        missing = required_fields - entry.keys()
        if missing:
            add_error(errors, f"manifest entry {index} is missing: {', '.join(sorted(missing))}")

        iso = entry.get("iso")
        if not isinstance(iso, str) or not ISO_PATTERN.fullmatch(iso):
            add_error(errors, f"manifest entry {index} has invalid ISO code: {iso!r}")
            continue
        if iso in isos:
            add_error(errors, f"duplicate ISO code in manifest: {iso}")
        isos.add(iso)

        coords = entry.get("coords")
        if not isinstance(coords, list) or len(coords) != 4:
            add_error(errors, f"{iso}: coords must contain four corners")
        elif any(
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(value, (int, float)) for value in point)
            for point in coords
        ):
            add_error(errors, f"{iso}: coords must contain numeric [longitude, latitude] pairs")

    return manifest


def validate_files(root: Path, manifest: list[dict[str, Any]], errors: list[str]) -> None:
    expected_isos = {entry["iso"] for entry in manifest if isinstance(entry.get("iso"), str)}
    prompts_dir = root / "prompts"
    masks_dir = root / "masks"

    prompt_isos = {path.stem for path in prompts_dir.glob("*.txt")}
    mask_isos = {path.stem for path in masks_dir.glob("*.png")}
    for label, actual in (("prompt", prompt_isos), ("mask", mask_isos)):
        for iso in sorted(expected_isos - actual):
            add_error(errors, f"{iso}: missing {label} file")
        for iso in sorted(actual - expected_isos):
            add_error(errors, f"{iso}: {label} file has no manifest entry")

    for entry in manifest:
        iso = entry.get("iso")
        if not isinstance(iso, str) or not ISO_PATTERN.fullmatch(iso):
            continue

        prompt_path = prompts_dir / f"{iso}.txt"
        if prompt_path.exists():
            prompt = prompt_path.read_text(encoding="utf-8")
            country_name = entry.get("en")
            if isinstance(country_name, str) and country_name not in prompt:
                add_error(errors, f"{iso}: prompt does not mention {country_name!r}")
            if "Everything OUTSIDE the silhouette is flat uniform magenta (#FF00FF)." not in prompt:
                add_error(errors, f"{iso}: prompt is missing the magenta-background instruction")

        mask_path = masks_dir / f"{iso}.png"
        if mask_path.exists():
            try:
                width, height, bit_depth, color_type = read_png_header(mask_path)
            except (OSError, ValueError) as exc:
                add_error(errors, f"{iso}: invalid mask PNG: {exc}")
                continue
            if (width, height) != (1024, 1024):
                add_error(errors, f"{iso}: mask must be 1024x1024, got {width}x{height}")
            if bit_depth != 8 or color_type not in {2, 6}:
                add_error(errors, f"{iso}: mask must be 8-bit RGB/RGBA PNG")


def validate_prompt_index(root: Path, manifest: list[dict[str, Any]], errors: list[str]) -> None:
    index_path = root / "prompts.md"
    try:
        index = index_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        add_error(errors, f"missing {index_path}")
        return

    expected_isos = [entry["iso"] for entry in manifest if isinstance(entry.get("iso"), str)]
    heading = re.search(r"^# mena: (\d+) countries$", index, flags=re.MULTILINE)
    if not heading:
        add_error(errors, "prompts.md is missing its country-count heading")
    elif int(heading.group(1)) != len(expected_isos):
        add_error(errors, "prompts.md country count does not match manifest.json")

    headings = re.findall(r"^## ([A-Z]{3}) —", index, flags=re.MULTILINE)
    if headings != expected_isos:
        add_error(errors, "prompts.md country order does not match manifest.json")

    for iso in expected_isos:
        if f"mask: `masks/{iso}.png` → save as `in/{iso}.png`" not in index:
            add_error(errors, f"prompts.md is missing the mask instruction for {iso}")
            continue

        section = re.search(
            rf"^## {iso} —.*?\nmask: `masks/{iso}\.png` → save as `in/{iso}\.png`\n\n```\n(.*?)\n```",
            index,
            flags=re.MULTILINE | re.DOTALL,
        )
        prompt_path = root / "prompts" / f"{iso}.txt"
        if not section or not prompt_path.exists():
            continue
        indexed_prompt = section.group(1).strip()
        file_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if indexed_prompt != file_prompt:
            add_error(errors, f"prompts.md prompt for {iso} differs from prompts/{iso}.txt")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = validate_manifest(root, errors)
    validate_files(root, manifest, errors)
    validate_prompt_index(root, manifest, errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "mena",
        help="path to the asset directory (default: ./mena)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    errors = validate(root)
    if errors:
        print(f"Asset validation failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    print(
        f"Validated {len(manifest)} countries, "
        f"{len(list((root / 'prompts').glob('*.txt')))} prompts, and "
        f"{len(list((root / 'masks').glob('*.png')))} masks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

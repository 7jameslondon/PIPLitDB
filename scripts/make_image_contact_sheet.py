"""Create a labeled contact sheet from supplementary image files."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    files = sorted(
        path
        for path in args.root.iterdir()
        if path.suffix.lower() in {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".webp"}
    )
    columns = 3
    rows = max(1, (len(files) + columns - 1) // columns)
    sheet = Image.new("RGB", (1500, rows * 560), "white")
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(files):
        with Image.open(path) as source:
            image = source.convert("RGB")
        image.thumbnail((470, 490))
        column = index % columns
        row = index // columns
        x = column * 500 + (500 - image.width) // 2
        y = row * 560 + 15
        sheet.paste(image, (x, y))
        draw.text((column * 500 + 12, row * 560 + 515), path.name, fill="black")
        draw.text(
            (column * 500 + 12, row * 560 + 535),
            f"{image.width}×{image.height} thumbnail",
            fill="black",
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output, quality=90)


if __name__ == "__main__":
    main()

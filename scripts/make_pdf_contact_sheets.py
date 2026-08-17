"""Build compact labeled contact sheets from rendered PDF page images."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pages-per-sheet", type=int, default=12)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    for folder in sorted(path for path in args.root.iterdir() if path.is_dir()):
        pages = sorted(folder.glob("*.png"))
        for start in range(0, len(pages), args.pages_per_sheet):
            chunk = pages[start : start + args.pages_per_sheet]
            sheet = Image.new("RGB", (1200, 1600), "white")
            draw = ImageDraw.Draw(sheet)
            for offset, page in enumerate(chunk):
                image = Image.open(page).convert("RGB")
                image.thumbnail((285, 450))
                column = offset % 4
                row = offset // 4
                x = column * 300 + (300 - image.width) // 2
                y = row * 520 + 30
                sheet.paste(image, (x, y))
                draw.text((column * 300 + 8, y + 455), page.stem, fill="black")
            sheet.save(
                args.output / f"{folder.name}_{start // args.pages_per_sheet + 1:02d}.jpg",
                quality=88,
            )


if __name__ == "__main__":
    main()

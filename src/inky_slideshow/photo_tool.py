from __future__ import annotations

import argparse
from pathlib import Path

from PIL import UnidentifiedImageError

from .slideshow import rotate_photo, validate_image


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("path")

    rotate_parser = subparsers.add_parser("rotate")
    rotate_parser.add_argument("path")
    rotate_parser.add_argument("direction", choices=["left", "right"])

    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_image(Path(args.path))
        elif args.command == "rotate":
            rotate_photo(Path(args.path), 90 if args.direction == "left" else -90)
    except (OSError, UnidentifiedImageError) as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()

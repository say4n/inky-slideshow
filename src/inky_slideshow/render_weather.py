from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from .slideshow import AppConfig, ConfigStore, WeatherClient, oriented_resolution, render_weather_screen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--resolution", default="800x480")
    args = parser.parse_args()

    width_text, height_text = args.resolution.lower().split("x", 1)
    resolution = (int(width_text), int(height_text))
    store = ConfigStore(Path(args.config), AppConfig())
    config = store.load()
    snapshot = WeatherClient().fetch_or_cached(config)
    image = render_weather_screen(oriented_resolution(resolution, config.frame_orientation), config, snapshot)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    sys.stdout.buffer.write(buffer.getvalue())


if __name__ == "__main__":
    main()

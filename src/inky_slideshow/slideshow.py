from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import click
from loguru import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - dependency is optional for import-only test environments
    register_heif_opener = None

if register_heif_opener is not None:
    register_heif_opener()

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".heif"}
DEFAULT_CONFIG_PATH = Path("~/.config/inky-slideshow/config.json").expanduser()
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
DEFAULT_LOCATION_NAME = "London"
DEFAULT_LATITUDE = 51.5072
DEFAULT_LONGITUDE = -0.1276
LONDON_TZ = "Europe/London"
KOLKATA_TZ = "Asia/Kolkata"
VALID_ORIENTATIONS = {"horizontal", "vertical"}
INKY_BLACK = "#111111"
INKY_BLUE = "#2563eb"
INKY_YELLOW = "#facc15"
INKY_RED = "#dc2626"
INKY_GREEN = "#16a34a"
INKY_ORANGE = "#ea580c"


@dataclass
class AppConfig:
    photo_seconds: int = 60
    weather_seconds: int = 30
    host: str = "0.0.0.0"
    port: int = 8080
    location_name: str = DEFAULT_LOCATION_NAME
    latitude: float = DEFAULT_LATITUDE
    longitude: float = DEFAULT_LONGITUDE
    frame_orientation: str = "horizontal"


@dataclass
class WeatherSnapshot:
    fetched_at: str
    location_name: str
    temperature_c: float | None
    feels_like_c: float | None
    weather_code: int | None
    wind_mph: float | None
    uv_index: float | None
    air_quality_index: int | None
    sunrise: str | None
    sunset: str | None
    hourly: list[dict[str, Any]]


class ConfigStore:
    def __init__(self, path: Path, defaults: AppConfig) -> None:
        self.path = path
        self.defaults = defaults
        self._lock = threading.RLock()

    def load(self) -> AppConfig:
        with self._lock:
            if not self.path.exists():
                self.save(self.defaults)
                return self.defaults
            try:
                data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                logger.exception("Failed to read config from {}", self.path)
                return self.defaults

            values = asdict(self.defaults)
            field_names = {field.name for field in fields(AppConfig)}
            values.update({key: value for key, value in data.items() if key in field_names})
            values["frame_orientation"] = normalize_orientation(values.get("frame_orientation"))
            return AppConfig(**values)

    def save(self, config: AppConfig) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True) + "\n")


class WeatherClient:
    def __init__(self) -> None:
        self._last_snapshot: WeatherSnapshot | None = None

    def fetch(self, config: AppConfig) -> WeatherSnapshot:
        forecast_params = {
            "latitude": config.latitude,
            "longitude": config.longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,weather_code,uv_index",
            "daily": "sunrise,sunset",
            "temperature_unit": "celsius",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 2,
        }
        air_params = {
            "latitude": config.latitude,
            "longitude": config.longitude,
            "hourly": "european_aqi",
            "timezone": "auto",
            "forecast_days": 1,
        }

        forecast = _fetch_json("https://api.open-meteo.com/v1/forecast", forecast_params)
        air_quality = _fetch_json("https://air-quality-api.open-meteo.com/v1/air-quality", air_params)
        snapshot = parse_weather(config.location_name, forecast, air_quality)
        self._last_snapshot = snapshot
        return snapshot

    def fetch_or_cached(self, config: AppConfig) -> WeatherSnapshot | None:
        try:
            return self.fetch(config)
        except Exception:
            logger.exception("Failed to fetch weather")
            return self._last_snapshot


def _fetch_json(url: str, params: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    request_url = f"{url}?{urlencode(params)}"
    with urlopen(request_url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_weather(location_name: str, forecast: dict[str, Any], air_quality: dict[str, Any]) -> WeatherSnapshot:
    current = forecast.get("current") or {}
    hourly = forecast.get("hourly") or {}
    daily = forecast.get("daily") or {}
    air_hourly = air_quality.get("hourly") or {}
    now = datetime.now(timezone.utc)
    hourly_items = _next_hourly_items(hourly, now, limit=10)

    uv_index = _nearest_hourly_value(hourly, "uv_index", now)
    aqi = _nearest_hourly_value(air_hourly, "european_aqi", now)

    return WeatherSnapshot(
        fetched_at=now.isoformat(),
        location_name=location_name,
        temperature_c=_optional_float(current.get("temperature_2m")),
        feels_like_c=_optional_float(current.get("apparent_temperature")),
        weather_code=_optional_int(current.get("weather_code")),
        wind_mph=_optional_float(current.get("wind_speed_10m")),
        uv_index=_optional_float(uv_index),
        air_quality_index=_optional_int(aqi),
        sunrise=_first_value(daily.get("sunrise")),
        sunset=_first_value(daily.get("sunset")),
        hourly=hourly_items,
    )


def _next_hourly_items(hourly: dict[str, list[Any]], now: datetime, limit: int) -> list[dict[str, Any]]:
    times = hourly.get("time") or []
    temperatures = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    items: list[dict[str, Any]] = []
    for index, value in enumerate(times):
        hour = _parse_open_meteo_time(value)
        if hour is None or hour < now.replace(tzinfo=None):
            continue
        items.append(
            {
                "time": value,
                "temperature_c": _list_value(temperatures, index),
                "weather_code": _list_value(codes, index),
            }
        )
        if len(items) >= limit:
            break
    return items


def _nearest_hourly_value(hourly: dict[str, list[Any]], key: str, now: datetime) -> Any:
    times = hourly.get("time") or []
    values = hourly.get(key) or []
    best_index = None
    best_delta = None
    naive_now = now.replace(tzinfo=None)
    for index, value in enumerate(times):
        parsed = _parse_open_meteo_time(value)
        if parsed is None:
            continue
        delta = abs((parsed - naive_now).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_index = index
    if best_index is None:
        return None
    return _list_value(values, best_index)


def _parse_open_meteo_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _list_value(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def _first_value(values: list[Any] | None) -> Any:
    return values[0] if values else None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_orientation(value: Any) -> str:
    return value if value in VALID_ORIENTATIONS else "horizontal"


def oriented_resolution(resolution: tuple[int, int], orientation: str) -> tuple[int, int]:
    short, long = sorted(resolution)
    return (long, short) if normalize_orientation(orientation) == "horizontal" else (short, long)


def image_for_display(image: Image.Image, display_resolution: tuple[int, int]) -> Image.Image:
    if image.size == display_resolution:
        return image
    if image.size == (display_resolution[1], display_resolution[0]):
        return image.rotate(90, expand=True)
    return ImageOps.contain(image, display_resolution)


def list_photos(photo_dir: Path) -> list[Path]:
    if not photo_dir.exists():
        return []
    return sorted(
        path
        for path in photo_dir.iterdir()
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
    )


def managed_photo_path(photo_dir: Path, filename: str) -> Path:
    safe_name = _safe_photo_filename(filename)
    if safe_name is None:
        raise ValueError("Invalid filename")
    target = (photo_dir / safe_name).resolve()
    root = photo_dir.resolve()
    if root != target.parent:
        raise ValueError("Invalid photo path")
    if target.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type")
    return target


def _safe_photo_filename(filename: str) -> str | None:
    if not filename or Path(filename).name != filename:
        return None
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)
    return cleaned if cleaned == filename else None


def fit_photo(path: Path, resolution: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image = ImageOps.contain(image.convert("RGB"), resolution)
        canvas = Image.new("RGB", resolution, "white")
        canvas.paste(image, ((resolution[0] - image.width) // 2, (resolution[1] - image.height) // 2))
        return canvas


def validate_image(path: Path) -> None:
    with Image.open(path) as image:
        image.verify()


def rotate_photo(path: Path, degrees: int) -> None:
    with Image.open(path) as image:
        rotated = ImageOps.exif_transpose(image).convert("RGB").rotate(degrees, expand=True)
        save_kwargs: dict[str, Any] = {}
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            save_kwargs = {"quality": 95, "subsampling": 0}
        rotated.save(path, **save_kwargs)





def render_weather_screen(
    resolution: tuple[int, int],
    config: AppConfig,
    snapshot: WeatherSnapshot | None,
    now: datetime | None = None,
) -> Image.Image:
    return render_weather_screen_pillow(resolution, config, snapshot, now)


def render_weather_screen_pillow(
    resolution: tuple[int, int],
    config: AppConfig,
    snapshot: WeatherSnapshot | None,
    now: datetime | None = None,
) -> Image.Image:
    now = now or datetime.now(ZoneInfo(LONDON_TZ))
    image = Image.new("RGB", resolution, "white")
    draw = ImageDraw.Draw(image)
    width, height = resolution
    is_vertical = height > width
    margin = 34 if not is_vertical else 28
    london_now = now.astimezone(ZoneInfo(LONDON_TZ))
    kolkata_now = now.astimezone(ZoneInfo(KOLKATA_TZ))

    date_font = _font(32 if not is_vertical else 26, "Bold")
    small_font = _font(22 if not is_vertical else 18, "Medium")
    label_font = _font(15 if not is_vertical else 14, "Bold")
    location_font = _font(26 if not is_vertical else 22, "Bold")
    temp_font = _font(118 if not is_vertical else 96, "Black")
    feels_font = _font(20 if not is_vertical else 18, "Medium")
    value_font = _font(30 if not is_vertical else 24, "Bold")
    unit_font = _font(68 if not is_vertical else 54, "Black")

    draw.text((margin, margin), london_now.strftime("%A, %-d %B"), font=date_font, fill=INKY_BLACK, anchor="lt")
    draw.rounded_rectangle((margin, margin + 40, margin + (232 if not is_vertical else 184), margin + 46), radius=3, fill=INKY_YELLOW)
    if not is_vertical:
        chip_right = width - margin
        chip_right = _draw_time_chip(draw, chip_right, margin + 2, f"KOLKATA {kolkata_now.strftime('%H:%M')}", small_font, INKY_GREEN)
        _draw_time_chip(draw, chip_right, margin + 2, f"LONDON {london_now.strftime('%H:%M')}", small_font, INKY_BLUE)
    else:
        draw.text((width - margin, margin + 3), f"LONDON {london_now.strftime('%H:%M')}", font=small_font, fill=INKY_BLUE, anchor="rt")
        draw.text((width - margin, margin + 28), f"KOLKATA {kolkata_now.strftime('%H:%M')}", font=small_font, fill=INKY_GREEN, anchor="rt")

    weather_code = snapshot.weather_code if snapshot else None
    weather_label = _weather_label(weather_code)
    temp_text = _format_temp(snapshot.temperature_c if snapshot else None)
    feels_text = f"Feels like {_format_temp(snapshot.feels_like_c if snapshot else None)}"

    if not is_vertical:
        top = 86
        icon_cx = 172
        icon_cy = 212
        _draw_weather_icon(draw, icon_cx, icon_cy, weather_code, 154)
        draw.text((icon_cx, 336), weather_label.upper(), font=label_font, fill=INKY_BLUE, anchor="mt")

        main_x = 340
        draw.text((main_x, top + 14), config.location_name, font=location_font, fill=INKY_BLACK, anchor="lt")
        _draw_temperature(draw, (main_x, top + 152), temp_text, temp_font, unit_font)
        draw.text((main_x + 6, top + 190), feels_text, font=feels_font, fill=INKY_BLACK, anchor="lt")
        draw.line((main_x, top + 232, width - margin, top + 232), fill=INKY_BLACK, width=3)

        metrics_y = top + 252
        metrics = [
            ("SUNRISE", _time_label(snapshot.sunrise) if snapshot else "--:--"),
            ("SUNSET", _time_label(snapshot.sunset) if snapshot else "--:--"),
            ("WIND", f"{_format_number(snapshot.wind_mph if snapshot else None)} mph"),
            ("UV", _uv_label(snapshot.uv_index if snapshot else None)),
        ]
        col_w = (width - main_x - margin - 14) // 2
        for i, (m_label, m_val) in enumerate(metrics):
            col = i % 2
            row = i // 2
            x = main_x + col * (col_w + 14)
            y = metrics_y + row * 62
            _draw_compact_metric(draw, (x, y), m_label, m_val, col_w, 54, label_font, value_font, _metric_color(m_label))
    else:
        top = 58
        icon_cx = width // 2
        _draw_weather_icon(draw, icon_cx, top + 104, weather_code, 132)
        draw.text((icon_cx, top + 200), weather_label.upper(), font=label_font, fill=INKY_BLUE, anchor="mt")
        draw.text((margin, top + 260), config.location_name, font=location_font, fill=INKY_BLACK, anchor="lt")
        _draw_temperature(draw, (margin, top + 390), temp_text, temp_font, unit_font)
        draw.text((margin + 4, top + 424), feels_text, font=feels_font, fill=INKY_BLACK, anchor="lt")
        draw.line((margin, top + 476, width - margin, top + 476), fill=INKY_BLACK, width=3)

        metrics = [
            ("SUNRISE", _time_label(snapshot.sunrise) if snapshot else "--:--"),
            ("SUNSET", _time_label(snapshot.sunset) if snapshot else "--:--"),
            ("WIND", f"{_format_number(snapshot.wind_mph if snapshot else None)} mph"),
            ("UV", _uv_label(snapshot.uv_index if snapshot else None)),
        ]
        metric_w = width - (margin * 2)
        for i, (m_label, m_val) in enumerate(metrics):
            _draw_compact_metric(draw, (margin, top + 504 + i * 62), m_label, m_val, metric_w, 50, label_font, value_font, _metric_color(m_label))

    if snapshot is None:
        draw.text((width - margin, height - margin), "WEATHER UNAVAILABLE", font=small_font, fill=INKY_RED, anchor="rb")

    return image

def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_DIR / f"Inter-{weight}.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _draw_time_chip(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    accent: str,
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0] + 32
    height = 34
    left = right - width
    draw.rounded_rectangle((left, y, right, y + height), radius=8, outline=accent, width=2)
    draw.rounded_rectangle((left + 8, y + 9, left + 15, y + height - 9), radius=3, fill=accent)
    draw.text((right - 12, y + height // 2), text, font=font, fill=INKY_BLACK, anchor="rm")
    return left - 12


def _draw_temperature(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: str,
    number_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    unit_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x, baseline = origin
    if text.endswith("C") and text != "--":
        number = text[:-1]
        draw.text((x, baseline), number, font=number_font, fill=INKY_BLACK, anchor="ls")
        bbox = draw.textbbox((x, baseline), number, font=number_font, anchor="ls")
        draw.text((bbox[2] + 8, baseline - 9), "C", font=unit_font, fill=INKY_RED, anchor="ls")
        return
    draw.text((x, baseline), text, font=number_font, fill=INKY_BLACK, anchor="ls")

def _draw_weather_icon(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    code: int | None,
    icon_size: int = 160,
) -> None:
    if code is None:
        code = 0
    if code == 0:
        char = "\uf00d"
    elif code in (1, 2):
        char = "\uf002"
    elif code == 3:
        char = "\uf013"
    elif code in (45, 48):
        char = "\uf014"
    elif code in range(51, 56) or code in range(56, 60):
        char = "\uf01c"
    elif code in range(61, 68):
        char = "\uf019"
    elif code in range(71, 78) or code in range(85, 87):
        char = "\uf01b"
    elif code in range(80, 83):
        char = "\uf01a"
    elif code >= 95:
        char = "\uf01e"
    else:
        char = "\uf00d"

    font_path = FONT_DIR / "weathericons.ttf"
    if font_path.exists():
        icon_font = ImageFont.truetype(str(font_path), icon_size)
        draw.text((cx + 4, cy + 4), char, font=icon_font, fill=INKY_BLACK, anchor="mm")
        draw.text((cx, cy), char, font=icon_font, fill=_weather_icon_color(code), anchor="mm")
    else:
        draw.ellipse((cx - 40, cy - 40, cx + 40, cy + 40), outline=_weather_icon_color(code), width=8)


def _draw_compact_metric(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    label: str,
    value: str,
    width: int,
    height: int,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    value_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    accent: str,
) -> None:
    x, y = origin
    draw.rounded_rectangle((x, y, x + width, y + height), radius=8, outline=INKY_BLACK, width=2)
    draw.rounded_rectangle((x + 5, y + 6, x + 14, y + height - 6), radius=4, fill=accent)
    if height <= 54:
        draw.text((x + 26, y + height // 2), label, font=label_font, fill=INKY_BLACK, anchor="lm")
        draw.text((x + width - 14, y + height // 2), value, font=value_font, fill=INKY_BLACK, anchor="rm")
        return
    draw.text((x + 28, y + 20), label, font=label_font, fill=INKY_BLACK, anchor="ls")
    draw.text((x + 28, y + height - 13), value, font=value_font, fill=INKY_BLACK, anchor="ls")


def _weather_icon_color(code: int | None) -> str:
    if code is None:
        return INKY_BLUE
    if code == 0 or code in (1, 2):
        return INKY_YELLOW
    if code >= 95:
        return INKY_RED
    if code in range(51, 68) or code in range(80, 83):
        return INKY_BLUE
    if code in range(71, 78) or code in range(85, 87):
        return INKY_BLUE
    return INKY_BLUE


def _metric_color(label: str) -> str:
    return {
        "SUNRISE": INKY_YELLOW,
        "SUNSET": INKY_RED,
        "WIND": INKY_BLUE,
        "UV": INKY_GREEN,
    }.get(label, INKY_BLACK)


def _weather_label(code: int | None) -> str:
    if code is None:
        return "Weather"
    if code == 0:
        return "Clear"
    if code in (1, 2):
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if code in range(51, 68) or code in range(80, 83):
        return "Rain"
    if code in range(71, 78) or code in range(85, 87):
        return "Snow"
    if code >= 95:
        return "Storm"
    return "Weather"


def _format_temp(value: float | None) -> str:
    return "--" if value is None else f"{round(value)}C"


def _format_number(value: float | None) -> str:
    return "--" if value is None else str(round(value))


def _uv_label(value: float | None) -> str:
    if value is None:
        return "--"
    level = "Low" if value < 3 else "Med" if value < 6 else "High"
    return f"{round(value)} {level}"


def _aqi_label(value: int | None) -> str:
    if value is None:
        return "--"
    if value < 20:
        level = "Good"
    elif value < 40:
        level = "Fair"
    elif value < 60:
        level = "Mod"
    elif value < 80:
        level = "Poor"
    else:
        level = "Bad"
    return f"{value} {level}"


def _time_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--:--" if parsed is None else parsed.strftime("%H:%M")


def start_display_worker(photo_dir: Path, config_store: ConfigStore) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: run_display_worker(photo_dir, config_store),
        name="inky-display",
        daemon=True,
    )
    thread.start()
    return thread


def run_display_worker(photo_dir: Path, config_store: ConfigStore) -> None:
    while True:
        try:
            run_display_loop(photo_dir, config_store)
        except Exception:
            logger.exception("Display loop failed; retrying in 30 seconds")
            time.sleep(30)


def run_display_loop(photo_dir: Path, config_store: ConfigStore) -> None:
    from inky.auto import auto

    inky_display = auto(ask_user=True)
    inky_display.set_border(inky_display.WHITE)
    weather_client = WeatherClient()
    index = random.randint(0, 100000)

    while True:
        config = config_store.load()
        photos = list_photos(photo_dir)
        target_resolution = oriented_resolution(inky_display.resolution, config.frame_orientation)
        if photos:
            current_image = photos[index % len(photos)]
            logger.info("Displaying photo: {}", current_image)
            try:
                image = fit_photo(current_image, target_resolution)
            except (OSError, UnidentifiedImageError):
                logger.exception("Skipping unreadable photo: {}", current_image)
            else:
                inky_display.set_image(image_for_display(image, inky_display.resolution))
                try:
                    inky_display.show()
                except Exception:
                    logger.exception("Display refresh failed while showing photo: {}", current_image)
                    raise
            index += 1
            time.sleep(config.photo_seconds)
        else:
            logger.warning("No photos found in {}", photo_dir)

        config = config_store.load()
        target_resolution = oriented_resolution(inky_display.resolution, config.frame_orientation)
        logger.info("Displaying weather screen")
        snapshot = weather_client.fetch_or_cached(config)
        weather_image = render_weather_screen(target_resolution, config, snapshot)
        inky_display.set_image(image_for_display(weather_image, inky_display.resolution))
        try:
            inky_display.show()
        except Exception:
            logger.exception("Display refresh failed while showing weather screen")
            raise
        time.sleep(config.weather_seconds)


@click.command()
@click.argument("path", type=click.Path(file_okay=False))
@click.option("--config", "config_path", type=click.Path(dir_okay=False), default=str(DEFAULT_CONFIG_PATH), show_default=True)
@click.option("--photo-seconds", default=60, show_default=True, type=int)
@click.option("--weather-seconds", default=30, show_default=True, type=int)
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--location-name", default=DEFAULT_LOCATION_NAME, show_default=True)
@click.option("--latitude", default=DEFAULT_LATITUDE, show_default=True, type=float)
@click.option("--longitude", default=DEFAULT_LONGITUDE, show_default=True, type=float)
@click.option("--frame-orientation", type=click.Choice(["horizontal", "vertical"]), default="horizontal", show_default=True)
@click.option("--mode", type=click.Choice(["display"]), default="display", show_default=True)
def main(
    path: str,
    config_path: str,
    photo_seconds: int,
    weather_seconds: int,
    host: str,
    port: int,
    location_name: str,
    latitude: float,
    longitude: float,
    frame_orientation: str,
    mode: str,
) -> None:
    photo_dir = Path(path)
    photo_dir.mkdir(parents=True, exist_ok=True)
    defaults = AppConfig(
        photo_seconds=max(1, photo_seconds),
        weather_seconds=max(1, weather_seconds),
        host=host,
        port=port,
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        frame_orientation=frame_orientation,
    )
    config_store = ConfigStore(Path(config_path).expanduser(), defaults)
    config_store.load()
    run_display_loop(photo_dir, config_store)


if __name__ == "__main__":
    main()

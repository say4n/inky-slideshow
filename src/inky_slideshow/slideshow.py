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
INKY_MUTED_BLUE = "#a5b4d6"
INKY_MUTED_YELLOW = "#ded49a"
INKY_MUTED_RED = "#d8aaa6"
INKY_MUTED_GREEN = "#a7c4ad"
INKY_MUTED_ORANGE = "#dec0a2"
INKY_DISPLAY_PALETTE = (
    (255, 255, 255),
    (0, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 0, 0),
    (255, 255, 0),
    (255, 128, 0),
)


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
    humidity_percent: float | None
    uv_index: float | None
    air_quality_index: int | None
    today_low_c: float | None
    today_high_c: float | None
    tomorrow_low_c: float | None
    tomorrow_high_c: float | None
    tomorrow_weather_code: int | None
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
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "hourly": "temperature_2m,weather_code,uv_index",
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,weather_code",
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
        humidity_percent=_optional_float(current.get("relative_humidity_2m")),
        uv_index=_optional_float(uv_index),
        air_quality_index=_optional_int(aqi),
        today_low_c=_optional_float(_list_value(daily.get("temperature_2m_min") or [], 0)),
        today_high_c=_optional_float(_list_value(daily.get("temperature_2m_max") or [], 0)),
        tomorrow_low_c=_optional_float(_list_value(daily.get("temperature_2m_min") or [], 1)),
        tomorrow_high_c=_optional_float(_list_value(daily.get("temperature_2m_max") or [], 1)),
        tomorrow_weather_code=_optional_int(_list_value(daily.get("weather_code") or [], 1)),
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
    return dither_for_inky(render_weather_screen_pillow(resolution, config, snapshot, now))


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
    margin = 24 if not is_vertical else 22
    london_now = now.astimezone(ZoneInfo(LONDON_TZ))
    kolkata_now = now.astimezone(ZoneInfo(KOLKATA_TZ))

    weather_code = snapshot.weather_code if snapshot else None
    weather_label = _weather_label(weather_code)
    temp_text = _format_temp(snapshot.temperature_c if snapshot else None)
    feels_text = f"Feels like {_format_temp(snapshot.feels_like_c if snapshot else None)}"

    if not is_vertical:
        gap = 14
        current_box = (margin, margin, 378, 274)
        clock_box = (392, margin, width - margin, 274)
        bottom_y = 288
        footer_h = 34
        card_h = height - bottom_y - footer_h - margin - gap
        card_w = (width - (margin * 2) - (gap * 3)) // 4

        _draw_panel(draw, current_box, INKY_YELLOW, density="medium")
        _draw_panel(draw, clock_box, INKY_BLUE, density="light")
        _draw_current_card(draw, current_box, config.location_name, weather_code, weather_label, temp_text, feels_text)
        _draw_clock_card(draw, clock_box, london_now, kolkata_now)

        bottom_cards = [
            (
                "TODAY",
                _format_temp_range(snapshot.today_low_c if snapshot else None, snapshot.today_high_c if snapshot else None),
                f"{_time_label(snapshot.sunrise) if snapshot else '--:--'} / {_time_label(snapshot.sunset) if snapshot else '--:--'}",
                "\uf051",
                INKY_ORANGE,
            ),
            (
                "TOMORROW",
                _short_weather_label(snapshot.tomorrow_weather_code if snapshot else None),
                _format_temp_range(snapshot.tomorrow_low_c if snapshot else None, snapshot.tomorrow_high_c if snapshot else None),
                _weather_icon_char(snapshot.tomorrow_weather_code if snapshot else None),
                INKY_BLUE,
            ),
            (
                "COMFORT",
                f"{_format_number(snapshot.humidity_percent if snapshot else None)}%",
                f"UV {_uv_label(snapshot.uv_index if snapshot else None)}",
                "\uf07a",
                INKY_GREEN,
            ),
            (
                "AIR / WIND",
                _aqi_label(snapshot.air_quality_index if snapshot else None),
                f"{_format_number(snapshot.wind_mph if snapshot else None)} mph",
                "\uf050",
                INKY_RED,
            ),
        ]
        for index, card in enumerate(bottom_cards):
            x = margin + index * (card_w + gap)
            _draw_info_card(draw, (x, bottom_y, x + card_w, bottom_y + card_h), *card)

        footer_box = (margin, height - margin - footer_h, width - margin, height - margin)
        _draw_footer(draw, footer_box, _admin_url_label(config), london_now)
    else:
        gap = 12
        current_box = (margin, margin, width - margin, 282)
        clock_box = (margin, 294, width - margin, 424)
        _draw_panel(draw, current_box, INKY_YELLOW, density="medium")
        _draw_panel(draw, clock_box, INKY_BLUE, density="light")
        _draw_current_card(draw, current_box, config.location_name, weather_code, weather_label, temp_text, feels_text, compact=True)
        _draw_clock_card(draw, clock_box, london_now, kolkata_now, compact=True)

        card_y = 436
        card_h = 62
        cards = [
            ("TODAY", _format_temp_range(snapshot.today_low_c if snapshot else None, snapshot.today_high_c if snapshot else None), "LOW / HIGH", "\uf053", INKY_ORANGE),
            ("TOMORROW", _short_weather_label(snapshot.tomorrow_weather_code if snapshot else None), _format_temp_range(snapshot.tomorrow_low_c if snapshot else None, snapshot.tomorrow_high_c if snapshot else None), _weather_icon_char(snapshot.tomorrow_weather_code if snapshot else None), INKY_BLUE),
            ("COMFORT", f"{_format_number(snapshot.humidity_percent if snapshot else None)}%", f"UV {_uv_label(snapshot.uv_index if snapshot else None)}", "\uf07a", INKY_GREEN),
            ("AIR / WIND", _aqi_label(snapshot.air_quality_index if snapshot else None), f"{_format_number(snapshot.wind_mph if snapshot else None)} mph", "\uf050", INKY_RED),
        ]
        for index, card in enumerate(cards):
            _draw_info_card(draw, (margin, card_y + index * (card_h + gap), width - margin, card_y + index * (card_h + gap) + card_h), *card, compact=True)
        _draw_footer(draw, (margin, height - margin - 30, width - margin, height - margin), _admin_url_label(config), london_now)

    if snapshot is None:
        draw.text((width - margin - 8, height - margin - 8), "WEATHER UNAVAILABLE", font=_font(16, "Bold"), fill=INKY_RED, anchor="rb")

    return image


def dither_for_inky(image: Image.Image) -> Image.Image:
    palette = Image.new("P", (1, 1))
    palette_values: list[int] = []
    for red, green, blue in INKY_DISPLAY_PALETTE:
        palette_values.extend((red, green, blue))
    palette_values.extend([0] * (768 - len(palette_values)))
    palette.putpalette(palette_values)
    return image.convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_DIR / f"Inter-{weight}.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str, density: str = "light") -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=12, outline=INKY_BLACK, width=3, fill="white")
    inset = 7
    spacing = 4 if density == "light" else 3
    radius = 2 if density == "light" else 2
    _draw_halftone(draw, (x1 + inset, y1 + inset, x2 - inset, y2 - inset), spacing=spacing, radius=radius, fill=_muted_accent(accent))


def _draw_halftone(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    spacing: int = 6,
    radius: int = 1,
    fill: str = INKY_BLACK,
    offset: int = 0,
) -> None:
    x1, y1, x2, y2 = box
    for row, y in enumerate(range(y1, y2, spacing)):
        shift = spacing // 2 if (row + offset) % 2 else 0
        for x in range(x1 + shift, x2, spacing):
            draw.ellipse((x, y, x + radius, y + radius), fill=fill)


def _muted_accent(accent: str) -> str:
    return {
        INKY_BLUE: INKY_MUTED_BLUE,
        INKY_YELLOW: INKY_MUTED_YELLOW,
        INKY_RED: INKY_MUTED_RED,
        INKY_GREEN: INKY_MUTED_GREEN,
        INKY_ORANGE: INKY_MUTED_ORANGE,
    }.get(accent, accent)


def _draw_current_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    location: str,
    weather_code: int | None,
    weather_label: str,
    temp_text: str,
    feels_text: str,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    title_font = _font(18 if not compact else 16, "Bold")
    temp_font = _font(92 if not compact else 78, "Black")
    unit_font = _font(54 if not compact else 46, "Black")
    body_font = _font(18 if not compact else 16, "Medium")
    label_font = _font(16 if not compact else 14, "Bold")
    icon_size = 118 if not compact else 94

    draw.text((x1 + 22, y1 + 22), location, font=title_font, fill=INKY_BLACK, anchor="lt")
    _draw_weather_icon(draw, x1 + 94, y1 + (134 if not compact else 118), weather_code, icon_size)
    _draw_temperature(draw, (x1 + 178, y1 + (138 if not compact else 126)), temp_text, temp_font, unit_font)
    _draw_fitted_text(draw, feels_text, (x1 + 180, y1 + (156 if not compact else 143), x2 - 18, y1 + (184 if not compact else 168)), body_font, INKY_BLACK, "lm")
    _draw_fitted_text(draw, weather_label.upper(), (x1 + 24, y2 - 42, x2 - 22, y2 - 17), label_font, INKY_BLACK, "lm")


def _draw_clock_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    london_now: datetime,
    kolkata_now: datetime,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    date_font = _font(42 if not compact else 30, "Black")
    day_font = _font(22 if not compact else 18, "Bold")
    label_font = _font(16 if not compact else 14, "Bold")
    time_font = _font(42 if not compact else 32, "Black")
    small_font = _font(15 if not compact else 13, "Medium")

    draw.text((x1 + 24, y1 + 24), london_now.strftime("%A").upper(), font=day_font, fill=INKY_BLACK, anchor="lt")
    draw.text((x1 + 24, y1 + (88 if not compact else 70)), london_now.strftime("%-d %B"), font=date_font, fill=INKY_BLACK, anchor="lm")
    draw.text((x1 + 24, y1 + (130 if not compact else 100)), london_now.strftime("%Y"), font=small_font, fill=INKY_BLACK, anchor="lt")

    row_y = y1 + (164 if not compact else 58)
    rows = [
        ("LONDON", london_now.strftime("%H:%M"), INKY_BLUE),
        ("KOLKATA", kolkata_now.strftime("%H:%M"), INKY_GREEN),
    ]
    for index, (label, value, accent) in enumerate(rows):
        if compact:
            x = x1 + 210 + index * 132
            _draw_time_cell(draw, (x, y1 + 44, x + 116, y2 - 18), label, value, accent, label_font, _font(28, "Black"))
        else:
            _draw_time_cell(draw, (x1 + 24 + index * 172, row_y, x1 + 176 + index * 172, y2 - 22), label, value, accent, label_font, time_font)


def _draw_time_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    accent: str,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    value_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=8, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y2 - 6), spacing=4, radius=2, fill=_muted_accent(accent), offset=1)
    draw.text((x1 + 12, y1 + 13), label, font=label_font, fill=INKY_BLACK, anchor="lt")
    _draw_fitted_text(draw, value, (x1 + 12, y1 + 30, x2 - 10, y2 - 8), value_font, INKY_BLACK, "lb")


def _draw_info_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    detail: str,
    icon: str,
    accent: str,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=10, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y1 + 27), spacing=4, radius=2, fill=_muted_accent(accent))
    label_font = _font(13 if not compact else 12, "Bold")
    value_font = _font(25 if not compact else 20, "Black")
    detail_font = _font(14 if not compact else 12, "Medium")
    icon_font = _weather_font(24 if not compact else 20)

    draw.text((x1 + 12, y1 + 12), label, font=label_font, fill=INKY_BLACK, anchor="lt")
    draw.text((x2 - 12, y1 + 18), icon, font=icon_font, fill=INKY_BLACK, anchor="mm")
    _draw_fitted_text(draw, value, (x1 + 12, y1 + 35, x2 - 12, y1 + 68), value_font, INKY_BLACK, "lb")
    _draw_fitted_text(draw, detail, (x1 + 12, y2 - 25, x2 - 12, y2 - 8), detail_font, INKY_BLACK, "lb")


def _draw_footer(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], admin_url: str, now: datetime) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=8, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y2 - 6), spacing=4, radius=2, fill=INKY_MUTED_BLUE)
    font = _font(14, "Bold")
    draw.text((x1 + 14, y1 + (y2 - y1) // 2), f"ADMIN {admin_url}", font=font, fill=INKY_BLACK, anchor="lm")
    draw.text((x2 - 14, y1 + (y2 - y1) // 2), f"UPDATED {now.strftime('%H:%M')}", font=font, fill=INKY_BLACK, anchor="rm")


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    anchor: str,
) -> None:
    x1, y1, x2, y2 = box
    candidate = font
    while isinstance(candidate, ImageFont.FreeTypeFont):
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if bbox[2] - bbox[0] <= x2 - x1 and bbox[3] - bbox[1] <= y2 - y1:
            break
        if candidate.size <= 10:
            break
        candidate = _font(candidate.size - 1, _font_weight(candidate))
    anchor_x = x1 if "l" in anchor else x2 if "r" in anchor else (x1 + x2) // 2
    anchor_y = y1 if "t" in anchor else y2 if "b" in anchor else (y1 + y2) // 2
    draw.text((anchor_x, anchor_y), text, font=candidate, fill=fill, anchor=anchor)


def _font_weight(font: ImageFont.FreeTypeFont) -> str:
    name = font.getname()[1]
    if "Black" in name:
        return "Black"
    if "Bold" in name:
        return "Bold"
    if "Medium" in name:
        return "Medium"
    return "Regular"


def _weather_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_DIR / "weathericons.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


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
    char = _weather_icon_char(code)

    font_path = FONT_DIR / "weathericons.ttf"
    if font_path.exists():
        icon_font = ImageFont.truetype(str(font_path), icon_size)
        draw.text((cx + 4, cy + 4), char, font=icon_font, fill=INKY_BLACK, anchor="mm")
        draw.text((cx, cy), char, font=icon_font, fill=_weather_icon_color(code), anchor="mm")
    else:
        draw.ellipse((cx - 40, cy - 40, cx + 40, cy + 40), outline=_weather_icon_color(code), width=8)


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


def _weather_icon_char(code: int | None) -> str:
    if code is None:
        return "\uf00d"
    if code == 0:
        return "\uf00d"
    if code in (1, 2):
        return "\uf002"
    if code == 3:
        return "\uf013"
    if code in (45, 48):
        return "\uf014"
    if code in range(51, 56) or code in range(56, 60):
        return "\uf01c"
    if code in range(61, 68):
        return "\uf019"
    if code in range(71, 78) or code in range(85, 87):
        return "\uf01b"
    if code in range(80, 83):
        return "\uf01a"
    if code >= 95:
        return "\uf01e"
    return "\uf00d"


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


def _short_weather_label(code: int | None) -> str:
    label = _weather_label(code)
    return "Cloudy" if label == "Partly cloudy" else label


def _format_temp(value: float | None) -> str:
    return "--" if value is None else f"{round(value)}C"


def _format_temp_range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "--"
    return f"{round(low)}-{round(high)}C"


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


def _admin_url_label(config: AppConfig) -> str:
    if config.host in {"0.0.0.0", "::", ""}:
        return f"zero-frame.local:{config.port}"
    return f"{config.host}:{config.port}"


def start_display_worker(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: Any | None = None,
) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: run_display_worker(photo_dir, config_store, photo_lock, weather_cache),
        name="inky-display",
        daemon=True,
    )
    thread.start()
    return thread


def run_display_worker(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: Any | None = None,
) -> None:
    while True:
        try:
            run_display_loop(photo_dir, config_store, photo_lock, weather_cache)
        except Exception:
            logger.exception("Display loop failed; retrying in 30 seconds")
            time.sleep(30)


def run_display_loop(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: Any | None = None,
) -> None:
    from inky.auto import auto

    lock = photo_lock or threading.RLock()
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
                with lock:
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
        snapshot = weather_cache.get(config) if weather_cache is not None else weather_client.fetch_or_cached(config)
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
@click.option("--mode", type=click.Choice(["combined", "display", "admin"]), default="combined", show_default=True)
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
    photo_lock = threading.RLock()
    if mode == "display":
        run_display_loop(photo_dir, config_store, photo_lock)
    else:
        from .admin import WeatherCache, run_admin_server

        weather_cache = WeatherCache()
        if mode == "combined":
            start_display_worker(photo_dir, config_store, photo_lock, weather_cache)
        run_admin_server(photo_dir, config_store, photo_lock, weather_cache)


if __name__ == "__main__":
    main()

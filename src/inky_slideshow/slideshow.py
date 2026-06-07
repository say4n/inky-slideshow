from __future__ import annotations

import json
import math
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
from flask import Flask, Response, abort, redirect, render_template_string, request, send_from_directory, url_for
from loguru import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename

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
DEFAULT_LOCATION_NAME = "London"
DEFAULT_LATITUDE = 51.5072
DEFAULT_LONGITUDE = -0.1276
LONDON_TZ = "Europe/London"
KOLKATA_TZ = "Asia/Kolkata"


@dataclass
class AppConfig:
    photo_seconds: int = 60
    weather_seconds: int = 30
    host: str = "0.0.0.0"
    port: int = 8080
    location_name: str = DEFAULT_LOCATION_NAME
    latitude: float = DEFAULT_LATITUDE
    longitude: float = DEFAULT_LONGITUDE


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


def list_photos(photo_dir: Path) -> list[Path]:
    if not photo_dir.exists():
        return []
    return sorted(
        path
        for path in photo_dir.iterdir()
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
    )


def managed_photo_path(photo_dir: Path, filename: str) -> Path:
    safe_name = secure_filename(filename)
    if not safe_name or Path(safe_name).name != filename:
        raise ValueError("Invalid filename")
    target = (photo_dir / safe_name).resolve()
    root = photo_dir.resolve()
    if root != target.parent:
        raise ValueError("Invalid photo path")
    if target.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type")
    return target


def fit_photo(path: Path, resolution: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        image = ImageOps.contain(image.convert("RGB"), resolution)
        canvas = Image.new("RGB", resolution, "white")
        canvas.paste(image, ((resolution[0] - image.width) // 2, (resolution[1] - image.height) // 2))
        return canvas


def validate_image(path: Path) -> None:
    with Image.open(path) as image:
        image.verify()


def render_weather_screen(
    resolution: tuple[int, int],
    config: AppConfig,
    snapshot: WeatherSnapshot | None,
    now: datetime | None = None,
) -> Image.Image:
    now = now or datetime.now(ZoneInfo(LONDON_TZ))
    image = Image.new("RGB", resolution, "white")
    draw = ImageDraw.Draw(image)
    width, height = resolution
    scale = min(width / 800, height / 480)

    def s(value: int) -> int:
        return max(1, int(value * scale))

    title_font = _font(s(42))
    large_font = _font(s(64))
    medium_font = _font(s(28))
    small_font = _font(s(20))
    tiny_font = _font(s(16))

    draw.rectangle((s(8), s(8), width - s(8), height - s(8)), outline="black", width=s(2))
    draw.text((width // 2, s(26)), now.strftime("%A, %-d %B"), font=title_font, fill="black", anchor="ma")

    _draw_weather_icon(draw, (s(118), s(156)), s(82), snapshot.weather_code if snapshot else None)
    temperature = _format_temp(snapshot.temperature_c if snapshot else None)
    feels_like = _format_temp(snapshot.feels_like_c if snapshot else None)
    draw.text((s(290), s(122)), temperature, font=large_font, fill="black", anchor="la")
    draw.text((s(292), s(188)), f"Feels like {feels_like}", font=small_font, fill="black", anchor="la")

    london_now = now.astimezone(ZoneInfo(LONDON_TZ))
    kolkata_now = now.astimezone(ZoneInfo(KOLKATA_TZ))
    draw.text((s(475), s(120)), "London", font=medium_font, fill="black", anchor="la")
    draw.text((s(665), s(120)), london_now.strftime("%H:%M"), font=large_font, fill="black", anchor="ma")
    draw.text((s(475), s(170)), "Kolkata", font=medium_font, fill="black", anchor="la")
    draw.text((s(665), s(170)), kolkata_now.strftime("%H:%M"), font=large_font, fill="black", anchor="ma")

    hourly = snapshot.hourly if snapshot else []
    x_start = s(56)
    x_step = s(74)
    y_time = s(262)
    y_icon = s(302)
    draw.text((x_start, y_time), "Now", font=medium_font, fill="black", anchor="ma")
    _draw_weather_icon(draw, (x_start, y_icon), s(20), snapshot.weather_code if snapshot else None)
    for offset, item in enumerate(hourly[:9], start=1):
        x = x_start + x_step * offset
        hour_text = _hour_label(item.get("time"))
        draw.text((x, y_time), hour_text, font=medium_font, fill="black", anchor="ma")
        _draw_weather_icon(draw, (x, y_icon), s(20), _optional_int(item.get("weather_code")))

    sunrise = _time_label(snapshot.sunrise) if snapshot else "--:--"
    sunset = _time_label(snapshot.sunset) if snapshot else "--:--"
    _draw_sunrise(draw, (s(72), s(377)), s(38))
    draw.text((s(45), s(416)), "Sunrise", font=small_font, fill="black", anchor="la")
    draw.text((s(45), s(446)), sunrise, font=medium_font, fill="black", anchor="la")
    _draw_sunset(draw, (s(202), s(377)), s(44))
    draw.text((s(170), s(416)), "Sunset", font=small_font, fill="black", anchor="la")
    draw.text((s(170), s(446)), sunset, font=medium_font, fill="black", anchor="la")

    draw.text((s(320), s(372)), "wind", font=small_font, fill="black", anchor="ma")
    _draw_wind(draw, (s(322), s(342)), s(42))
    draw.text((s(320), s(420)), _format_number(snapshot.wind_mph if snapshot else None), font=medium_font, fill="black", anchor="ma")
    draw.text((s(320), s(450)), "mph", font=small_font, fill="black", anchor="ma")

    _draw_uv(draw, (s(435), s(338)), s(34))
    draw.text((s(435), s(415)), "UV Index", font=small_font, fill="black", anchor="ma")
    draw.text((s(435), s(448)), _uv_label(snapshot.uv_index if snapshot else None), font=medium_font, fill="black", anchor="ma")

    _draw_aqi(draw, (s(585), s(342)), s(42))
    draw.text((s(585), s(415)), "Air Quality", font=small_font, fill="black", anchor="ma")
    draw.text((s(585), s(448)), _aqi_label(snapshot.air_quality_index if snapshot else None), font=medium_font, fill="black", anchor="ma")

    if snapshot is None:
        draw.text((width // 2, height - s(32)), "weather unavailable", font=tiny_font, fill="black", anchor="ma")

    return image


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_name in ("DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _draw_weather_icon(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, code: int | None) -> None:
    x, y = center
    stroke = max(2, radius // 9)
    if code in {45, 48}:
        for offset in (-radius // 2, 0, radius // 2):
            draw.line((x - radius, y + offset, x + radius, y + offset), fill="black", width=stroke)
        return
    if code is not None and code >= 80:
        _draw_cloud(draw, center, radius, stroke)
        for offset in (-radius // 3, radius // 3):
            draw.line((x + offset, y + radius // 2, x + offset - radius // 5, y + radius), fill="black", width=stroke)
        return
    if code is not None and code >= 51:
        _draw_cloud(draw, center, radius, stroke)
        return
    if code is not None and code >= 1:
        draw.arc((x - radius, y - radius // 3, x + radius, y + radius), 180, 360, fill="black", width=stroke)
        draw.line((x - radius, y + radius // 3, x + radius, y + radius // 3), fill="black", width=stroke)
        for angle in (210, 245, 295, 330):
            _ray(draw, x, y, radius, angle, stroke)
        return
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="black", width=stroke)
    for angle in range(0, 360, 45):
        _ray(draw, x, y, radius, angle, stroke)


def _draw_cloud(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, stroke: int) -> None:
    x, y = center
    draw.arc((x - radius, y - radius // 2, x, y + radius // 2), 180, 360, fill="black", width=stroke)
    draw.arc((x - radius // 3, y - radius, x + radius // 2, y + radius // 4), 180, 360, fill="black", width=stroke)
    draw.arc((x, y - radius // 2, x + radius, y + radius // 2), 180, 360, fill="black", width=stroke)
    draw.line((x - radius, y, x + radius, y), fill="black", width=stroke)


def _ray(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int, angle: int, stroke: int) -> None:
    radians = math.radians(angle)
    inner = radius + radius // 4
    outer = radius + radius // 2
    draw.line(
        (
            x + int(math.cos(radians) * inner),
            y + int(math.sin(radians) * inner),
            x + int(math.cos(radians) * outer),
            y + int(math.sin(radians) * outer),
        ),
        fill="black",
        width=stroke,
    )


def _draw_sunrise(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    x, y = center
    draw.arc((x - radius, y - radius, x + radius, y + radius), 180, 360, fill="black", width=max(2, radius // 10))
    draw.line((x - radius, y, x + radius, y), fill="black", width=max(2, radius // 10))
    draw.line((x, y - radius - 16, x, y - radius // 2), fill="black", width=max(2, radius // 10))
    draw.line((x - 8, y - radius - 8, x, y - radius - 16, x + 8, y - radius - 8), fill="black", width=max(2, radius // 12))


def _draw_sunset(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    x, y = center
    draw.arc((x - radius, y - radius, x + radius, y + radius), 180, 360, fill="black", width=max(2, radius // 10))
    draw.line((x - radius, y, x + radius, y), fill="black", width=max(2, radius // 10))
    draw.line((x, y - radius - 16, x, y - radius // 2), fill="black", width=max(2, radius // 10))
    draw.line((x - 8, y - radius - 8, x, y - radius, x + 8, y - radius - 8), fill="black", width=max(2, radius // 12))


def _draw_wind(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    x, y = center
    width = max(2, radius // 12)
    for offset in (-radius // 3, 0, radius // 3):
        draw.line((x - radius, y + offset, x + radius // 2, y + offset), fill="black", width=width)
        draw.arc((x + radius // 3, y + offset - radius // 4, x + radius, y + offset + radius // 4), 270, 90, fill="black", width=width)


def _draw_uv(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    x, y = center
    draw.rounded_rectangle((x - radius, y - radius, x + radius, y + radius), radius=radius // 4, outline="black", width=max(2, radius // 10))
    font = _font(radius)
    draw.text((x, y), "UV", font=font, fill="black", anchor="mm")


def _draw_aqi(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    x, y = center
    draw.arc((x - radius, y - radius, x + radius, y + radius), 180, 360, fill="black", width=max(2, radius // 10))
    for angle in range(200, 341, 35):
        _ray(draw, x, y, radius // 2, angle, max(2, radius // 16))
    draw.rectangle((x - radius, y, x + radius, y + radius // 4), fill="white", outline="black", width=max(2, radius // 10))


def _format_temp(value: float | None) -> str:
    return "--" if value is None else f"{round(value)} deg"


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


def _hour_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--" if parsed is None else parsed.strftime("%H")


def _time_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--:--" if parsed is None else parsed.strftime("%H:%M")


def create_app(photo_dir: Path, config_store: ConfigStore) -> Flask:
    app = Flask(__name__)
    photo_dir.mkdir(parents=True, exist_ok=True)

    @app.route("/", methods=["GET"])
    def index() -> str:
        config = config_store.load()
        photos = [path.name for path in list_photos(photo_dir)]
        return render_template_string(ADMIN_TEMPLATE, config=config, photos=photos)

    @app.route("/settings", methods=["POST"])
    def settings() -> Response:
        current = config_store.load()
        updated = AppConfig(
            photo_seconds=_positive_int(request.form.get("photo_seconds"), current.photo_seconds),
            weather_seconds=_positive_int(request.form.get("weather_seconds"), current.weather_seconds),
            host=current.host,
            port=current.port,
            location_name=(request.form.get("location_name") or current.location_name).strip() or current.location_name,
            latitude=_float_value(request.form.get("latitude"), current.latitude),
            longitude=_float_value(request.form.get("longitude"), current.longitude),
        )
        config_store.save(updated)
        return redirect(url_for("index"))

    @app.route("/photos", methods=["POST"])
    def upload_photo() -> Response:
        upload = request.files.get("photo")
        if upload is None or not upload.filename:
            abort(400, "No photo uploaded")
        try:
            target = managed_photo_path(photo_dir, secure_filename(upload.filename))
        except ValueError as error:
            abort(400, str(error))
        upload.save(target)
        try:
            validate_image(target)
        except (OSError, UnidentifiedImageError):
            target.unlink(missing_ok=True)
            abort(400, "Uploaded file is not a readable image")
        return redirect(url_for("index"))

    @app.route("/photos/<path:filename>", methods=["GET"])
    def photo(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            abort(404)
        if not target.exists():
            abort(404)
        return send_from_directory(photo_dir, target.name)

    @app.route("/photos/<path:filename>/delete", methods=["POST"])
    def delete_photo(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            abort(404)
        if target.exists():
            target.unlink()
        return redirect(url_for("index"))

    return app


def _positive_int(value: str | None, fallback: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _float_value(value: str | None, fallback: float) -> float:
    try:
        return float(value or "")
    except ValueError:
        return fallback


def run_web_server(photo_dir: Path, config_store: ConfigStore) -> threading.Thread:
    config = config_store.load()
    app = create_app(photo_dir, config_store)
    thread = threading.Thread(
        target=lambda: app.run(host=config.host, port=config.port, threaded=True, use_reloader=False),
        name="inky-admin-web",
        daemon=True,
    )
    thread.start()
    logger.info("Admin UI listening on http://{}:{}", config.host, config.port)
    return thread


def run_display_loop(photo_dir: Path, config_store: ConfigStore) -> None:
    from inky.auto import auto

    inky_display = auto(ask_user=True)
    inky_display.set_border(inky_display.WHITE)
    weather_client = WeatherClient()
    index = random.randint(0, 100000)

    while True:
        config = config_store.load()
        photos = list_photos(photo_dir)
        if photos:
            current_image = photos[index % len(photos)]
            logger.info("Displaying photo: {}", current_image)
            try:
                image = fit_photo(current_image, inky_display.resolution)
                inky_display.set_image(image)
                inky_display.show()
            except (OSError, UnidentifiedImageError):
                logger.exception("Skipping unreadable photo: {}", current_image)
            index += 1
            time.sleep(config.photo_seconds)
        else:
            logger.warning("No photos found in {}", photo_dir)

        config = config_store.load()
        logger.info("Displaying weather screen")
        snapshot = weather_client.fetch_or_cached(config)
        weather_image = render_weather_screen(inky_display.resolution, config, snapshot)
        inky_display.set_image(weather_image)
        inky_display.show()
        time.sleep(config.weather_seconds)


ADMIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Inky Slideshow</title>
    <style>
      :root { color-scheme: light; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; background: #f7f7f2; color: #111; }
      main { max-width: 960px; margin: 0 auto; padding: 24px; }
      h1 { font-size: 28px; margin: 0 0 20px; }
      h2 { font-size: 18px; margin: 0 0 12px; }
      section { margin: 0 0 28px; }
      form { display: grid; gap: 12px; }
      label { display: grid; gap: 4px; font-weight: 600; }
      input { font: inherit; padding: 9px 10px; border: 1px solid #999; border-radius: 6px; background: white; }
      button { font: inherit; border: 1px solid #111; background: #111; color: white; padding: 9px 12px; border-radius: 6px; cursor: pointer; }
      .settings { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); align-items: end; }
      .photos { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 14px; }
      .photo { background: white; border: 1px solid #ccc; border-radius: 8px; padding: 8px; }
      .photo img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; background: #eee; margin-bottom: 8px; }
      .photo form { display: block; }
      .muted { color: #555; }
    </style>
  </head>
  <body>
    <main>
      <h1>Inky Slideshow</h1>
      <section>
        <h2>Settings</h2>
        <form class="settings" action="/settings" method="post">
          <label>Photo seconds <input name="photo_seconds" type="number" min="1" value="{{ config.photo_seconds }}"></label>
          <label>Weather seconds <input name="weather_seconds" type="number" min="1" value="{{ config.weather_seconds }}"></label>
          <label>Location name <input name="location_name" value="{{ config.location_name }}"></label>
          <label>Latitude <input name="latitude" type="number" step="0.0001" value="{{ config.latitude }}"></label>
          <label>Longitude <input name="longitude" type="number" step="0.0001" value="{{ config.longitude }}"></label>
          <button type="submit">Save</button>
        </form>
      </section>
      <section>
        <h2>Upload Photo</h2>
        <form action="/photos" method="post" enctype="multipart/form-data">
          <input name="photo" type="file" accept=".png,.jpg,.jpeg,.heic,.heif,image/png,image/jpeg,image/heic,image/heif" required>
          <button type="submit">Upload</button>
        </form>
      </section>
      <section>
        <h2>Photos</h2>
        {% if photos %}
        <div class="photos">
          {% for photo in photos %}
          <div class="photo">
            <img src="{{ url_for('photo', filename=photo) }}" alt="{{ photo }}">
            <form action="{{ url_for('delete_photo', filename=photo) }}" method="post">
              <button type="submit">Delete</button>
            </form>
          </div>
          {% endfor %}
        </div>
        {% else %}
        <p class="muted">No photos uploaded yet.</p>
        {% endif %}
      </section>
    </main>
  </body>
</html>
"""


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
    )
    config_store = ConfigStore(Path(config_path).expanduser(), defaults)
    config_store.load()
    run_web_server(photo_dir, config_store)
    run_display_loop(photo_dir, config_store)


if __name__ == "__main__":
    main()

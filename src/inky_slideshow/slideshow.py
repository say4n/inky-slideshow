from __future__ import annotations

import io
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
VALID_ORIENTATIONS = {"horizontal", "vertical"}


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
    from PIL import ImageFilter
    now = now or datetime.now(ZoneInfo(LONDON_TZ))
    image = Image.new("RGB", resolution, "black")
    draw = ImageDraw.Draw(image)
    width, height = resolution

    is_vertical = height > width
    rx = int(width * 0.45) if not is_vertical else width
    ry = height if not is_vertical else int(height * 0.45)

    if not is_vertical:
        draw.rectangle((rx, 0, width, height), fill="white")
    else:
        draw.rectangle((0, ry, width, height), fill="white")

    london_now = now.astimezone(ZoneInfo(LONDON_TZ))
    kolkata_now = now.astimezone(ZoneInfo(KOLKATA_TZ))

    date_font = _font(32, "Black")
    loc_font = _font(24, "Regular")
    time_size = 110 if not is_vertical else 90
    time_font = _font(time_size, "Bold")
    cities_font = _font(16, "Medium")

    draw.text((40, 48), now.strftime("%A, %-d %b").upper(), font=date_font, fill="white", anchor="lt")
    draw.text((40, 88), config.location_name, font=loc_font, fill="white", anchor="lt")

    if not is_vertical:
        time_y = 120
        icon_x = rx // 2
        icon_y = height - 130
        icon_size = 160
    else:
        time_y = 130
        icon_x = width - 80
        icon_y = ry // 2 + 10
        icon_size = 120

    draw.text((40, time_y), london_now.strftime("%H:%M"), font=time_font, fill="white", anchor="lt")

    weather_code = snapshot.weather_code if snapshot else 0
    _draw_weather_icon(draw, icon_x, icon_y, weather_code, icon_size)

    bottom_y = height - 48 if not is_vertical else ry - 48
    draw.text((40, bottom_y), f"KOLKATA {kolkata_now.strftime('%H:%M')}", font=cities_font, fill="white", anchor="ls")

    temp_font = _font(140, "Black")
    feels_font = _font(24, "Medium")
    label_font = _font(14, "Bold")
    val_font = _font(36, "Black")

    temp_text = _format_temp(snapshot.temperature_c if snapshot else None)
    feels_text = f"FL {_format_temp(snapshot.feels_like_c if snapshot else None)}"

    right_x = rx if not is_vertical else 0
    right_y = 0 if not is_vertical else ry
    right_w = width - right_x

    temp_baseline = right_y + 160
    draw.text((right_x + 40, temp_baseline), temp_text, font=temp_font, fill="black", anchor="ls")
    draw.text((right_x + right_w - 40, temp_baseline), feels_text, font=feels_font, fill="black", anchor="rs")

    divider_y = temp_baseline + 24
    draw.rectangle((right_x + 40, divider_y, right_x + right_w - 40, divider_y + 4), fill="black")

    metrics = [
        ("SUNRISE", _time_label(snapshot.sunrise) if snapshot else "--:--"),
        ("SUNSET", _time_label(snapshot.sunset) if snapshot else "--:--"),
        ("WIND", f"{_format_number(snapshot.wind_mph if snapshot else None)} mph"),
        ("UV INDEX", _uv_label(snapshot.uv_index if snapshot else None)),
        ("AIR QUALITY", _aqi_label(snapshot.air_quality_index if snapshot else None)),
    ]

    my_y = divider_y + 40
    col_w = (right_w - 80 - 24) // 2
    for i, (m_label, m_val) in enumerate(metrics):
        col = i % 2
        row = i // 2
        x = right_x + 40 + col * (col_w + 24)
        y = my_y + row * 80
        # Thin crisp line instead of a yellow block
        draw.rectangle((x, y, x + 2, y + 45), fill="#fbbf24")
        draw.text((x + 16, y + 16), m_label, font=label_font, fill="black", anchor="ls")
        draw.text((x + 16, y + 46), m_val, font=val_font, fill="black", anchor="ls")

    if snapshot is None:
        draw.text((right_x + right_w // 2, height - 48), "weather unavailable", font=_font(14, "Bold"), fill="black", anchor="ms")

    return image

def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = Path(__file__).parent / "assets" / "fonts" / f"Inter-{weight}.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()

def _draw_weather_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, code: int | None, icon_size: int = 160) -> None:
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

    font_path = Path(__file__).parent / "assets" / "fonts" / "weathericons.ttf"
    if font_path.exists():
        icon_font = ImageFont.truetype(str(font_path), icon_size)
        draw.text((cx, cy), char, font=icon_font, fill="#fbbf24", anchor="mm")
    else:
        # Fallback
        draw.ellipse((cx - 40, cy - 40, cx + 40, cy + 40), fill="#fbbf24")


def _draw_metric(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    label: str,
    value: str,
    value_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
    height: int,
) -> None:
    x, y = origin
    draw.text((x, y + height // 2), label, font=label_font, fill="black", anchor="lm")
    draw.text((x + width, y + height // 2), value, font=value_font, fill="black", anchor="rm")
    draw.line((x, y + height, x + width, y + height), fill="black", width=max(1, height // 18))


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


def _hour_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--" if parsed is None else parsed.strftime("%H")


def _time_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--:--" if parsed is None else parsed.strftime("%H:%M")


def create_app(photo_dir: Path, config_store: ConfigStore) -> Flask:
    app = Flask(__name__)
    photo_dir.mkdir(parents=True, exist_ok=True)
    weather_client = WeatherClient()

    @app.route("/", methods=["GET"])
    def index() -> str:
        config = config_store.load()
        photos = [path.name for path in list_photos(photo_dir)]
        return render_template_string(ADMIN_TEMPLATE, config=config, photos=photos)

    @app.route("/weather-screen", methods=["GET"])
    def weather_screen() -> Response:
        config = config_store.load()
        snapshot = weather_client.fetch_or_cached(config)
        resolution = oriented_resolution((800, 480), config.frame_orientation)
        image = render_weather_screen(resolution, config, snapshot)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png")

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
            frame_orientation=normalize_orientation(request.form.get("frame_orientation") or current.frame_orientation),
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

    @app.route("/photos/<path:filename>/rotate", methods=["POST"])
    def rotate_photo_route(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            abort(404)
        if not target.exists():
            abort(404)
        direction = request.form.get("direction", "right")
        degrees = 90 if direction == "left" else -90
        try:
            rotate_photo(target, degrees)
        except (OSError, UnidentifiedImageError):
            abort(400, "Photo could not be rotated")
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


def run_web_server(photo_dir: Path, config_store: ConfigStore) -> None:
    config = config_store.load()
    app = create_app(photo_dir, config_store)
    logger.info("Admin UI listening on http://{}:{}", config.host, config.port)
    app.run(host=config.host, port=config.port, threaded=True, use_reloader=False)


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


ADMIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Inky Slideshow Console</title>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
      
      :root {
        --bg: #f8f9fa;
        --surface: #ffffff;
        --border: #eaedf0;
        --text-main: #111827;
        --text-muted: #6b7280;
        --accent: #000000;
        --accent-hover: #374151;
        --radius: 12px;
        --transition: 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      }
      
      * { box-sizing: border-box; }
      body { 
        margin: 0; 
        background: var(--bg); 
        color: var(--text-main);
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        -webkit-font-smoothing: antialiased;
      }
      main { max-width: 1000px; margin: 0 auto; padding: 48px 24px; }
      
      header { 
        display: flex; align-items: flex-end; justify-content: space-between; 
        margin-bottom: 40px; border-bottom: 1px solid var(--border);
        padding-bottom: 24px;
      }
      header h1 { font-size: 32px; font-weight: 800; letter-spacing: -0.02em; margin: 0; line-height: 1; }
      header p.muted { margin: 8px 0 0; font-size: 15px; color: var(--text-muted); }
      .weather-link { 
        color: var(--text-main); font-weight: 600; text-decoration: none; font-size: 14px;
        display: inline-flex; align-items: center; gap: 8px; transition: var(--transition);
        padding: 8px 16px; border-radius: 20px; background: var(--border);
      }
      .weather-link:hover { background: #e5e7eb; }
      
      .panel { 
        background: var(--surface); border-radius: var(--radius); 
        padding: 32px; box-shadow: 0 4px 24px -12px rgba(0,0,0,0.05);
        margin-bottom: 24px; border: 1px solid var(--border);
      }
      .panel h2 { font-size: 18px; font-weight: 700; margin: 0 0 24px; letter-spacing: -0.01em; }
      
      /* Form elements */
      .settings { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 20px; }
      label, .field { display: flex; flex-direction: column; gap: 8px; font-size: 14px; font-weight: 600; color: var(--text-muted); }
      
      input[type="text"], input[type="number"], input[type="file"] {
        width: 100%; font: inherit; padding: 12px 14px; color: var(--text-main);
        border: 1px solid #d1d5db; border-radius: 8px; background: var(--surface); 
        transition: var(--transition);
      }
      input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0,0,0,0.05); }
      
      button { 
        font: inherit; font-size: 14px; font-weight: 600; 
        border: 1px solid var(--accent); background: var(--accent); color: white; 
        padding: 12px 20px; border-radius: 8px; cursor: pointer; 
        transition: var(--transition);
      }
      button:hover { background: var(--accent-hover); border-color: var(--accent-hover); transform: translateY(-1px); }
      button:active { transform: translateY(0); }
      
      button.secondary { 
        background: var(--surface); color: var(--text-main); border-color: #d1d5db; 
        padding: 8px; width: 100%; font-size: 13px;
      }
      button.secondary:hover { background: #f9fafb; border-color: #9ca3af; color: var(--text-main); }
      
      .orientation { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
      .orientation input { position: absolute; opacity: 0; pointer-events: none; }
      .orientation span { 
        display: flex; align-items: center; justify-content: center;
        border: 1px solid #d1d5db; border-radius: 8px; padding: 10px; 
        color: var(--text-muted); cursor: pointer; transition: var(--transition);
      }
      .orientation input:checked + span { background: var(--accent); color: white; border-color: var(--accent); }
      
      .upload { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; }
      .upload input[type="file"] { padding: 9px 12px; }
      
      /* Photos Grid */
      .photos-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 20px; }
      .photos { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
      .photo { 
        background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); 
        padding: 12px; transition: var(--transition);
      }
      .photo:hover { box-shadow: 0 10px 30px -10px rgba(0,0,0,0.1); transform: translateY(-2px); }
      .photo img { 
        display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: contain; 
        background: #f3f4f6; border-radius: 6px; margin-bottom: 12px; 
      }
      .photos.vertical .photo img { aspect-ratio: 10 / 16; }
      .actions { display: grid; grid-template-columns: 1fr 1fr 1.5fr; gap: 8px; }
      .photo form { display: block; }
      
      @media (max-width: 768px) {
        .settings { grid-template-columns: 1fr 1fr; }
        .upload { grid-template-columns: 1fr; }
        .upload button { width: 100%; }
        header { flex-direction: column; align-items: flex-start; gap: 16px; }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <h1>Inky Console</h1>
          <p class="muted">Manage your photo gallery and weather display settings</p>
        </div>
        <a class="weather-link" href="/weather-screen" target="_blank">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
          Preview Display
        </a>
      </header>
      
      <section class="panel">
        <h2>Display Configuration</h2>
        <form class="settings" action="/settings" method="post">
          <label>Photo Duration (s) <input name="photo_seconds" type="number" min="1" value="{{ config.photo_seconds }}"></label>
          <label>Weather Duration (s) <input name="weather_seconds" type="number" min="1" value="{{ config.weather_seconds }}"></label>
          <div class="field">Frame Orientation
            <div class="orientation">
              <label><input name="frame_orientation" type="radio" value="horizontal" {% if config.frame_orientation == "horizontal" %}checked{% endif %}><span>Landscape</span></label>
              <label><input name="frame_orientation" type="radio" value="vertical" {% if config.frame_orientation == "vertical" %}checked{% endif %}><span>Portrait</span></label>
            </div>
          </div>
          <label>City Name <input name="location_name" value="{{ config.location_name }}"></label>
          <label>Latitude <input name="latitude" type="number" step="0.0001" value="{{ config.latitude }}"></label>
          <label>Longitude <input name="longitude" type="number" step="0.0001" value="{{ config.longitude }}"></label>
          <button type="submit" style="grid-column: 1 / -1; margin-top: 8px; width: 100%; max-width: 200px;">Save Settings</button>
        </form>
      </section>
      
      <section class="panel">
        <h2>Upload New Photo</h2>
        <form class="upload" action="/photos" method="post" enctype="multipart/form-data">
          <input name="photo" type="file" accept=".png,.jpg,.jpeg,.heic,.heif,image/png,image/jpeg,image/heic,image/heif" required>
          <button type="submit">Upload Photo</button>
        </form>
      </section>
      
      <section class="panel" style="background: transparent; border: none; box-shadow: none; padding: 0;">
        <div class="photos-head">
          <h2 style="margin:0;">Photo Gallery</h2>
          <p class="muted" style="margin:0;">{{ photos|length }} images loaded</p>
        </div>
        {% if photos %}
        <div class="photos {{ config.frame_orientation }}">
          {% for photo in photos %}
          <div class="photo">
            <img src="{{ url_for('photo', filename=photo) }}" alt="{{ photo }}">
            <div class="actions">
              <form action="{{ url_for('rotate_photo_route', filename=photo) }}" method="post">
                <input type="hidden" name="direction" value="left">
                <button class="secondary" type="submit" title="Rotate left">↺</button>
              </form>
              <form action="{{ url_for('rotate_photo_route', filename=photo) }}" method="post">
                <input type="hidden" name="direction" value="right">
                <button class="secondary" type="submit" title="Rotate right">↻</button>
              </form>
              <form action="{{ url_for('delete_photo', filename=photo) }}" method="post">
                <button class="secondary" type="submit" style="color: #ef4444; border-color: #fca5a5;">Trash</button>
              </form>
            </div>
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="panel" style="text-align: center; padding: 60px 20px;">
          <p class="muted">Your gallery is empty. Upload a photo to get started.</p>
        </div>
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
@click.option("--frame-orientation", type=click.Choice(["horizontal", "vertical"]), default="horizontal", show_default=True)
@click.option("--mode", type=click.Choice(["all", "web", "display"]), default="all", show_default=True)
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
    if mode == "web":
        run_web_server(photo_dir, config_store)
    elif mode == "display":
        run_display_loop(photo_dir, config_store)
    else:
        start_display_worker(photo_dir, config_store)
        run_web_server(photo_dir, config_store)


if __name__ == "__main__":
    main()

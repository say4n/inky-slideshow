from __future__ import annotations

import ctypes
import gc
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
from PIL import Image, ImageOps, UnidentifiedImageError

from .render_weather import render_weather_screen

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


@dataclass
class DisplayStatus:
    mode: str = "starting"
    detail: str | None = None
    started_at: str | None = None
    duration_seconds: int | None = None


class DisplayState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status = DisplayStatus()

    def update(self, mode: str, detail: str | None = None, duration_seconds: int | None = None) -> None:
        with self._lock:
            self._status = DisplayStatus(
                mode=mode,
                detail=detail,
                started_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=duration_seconds,
            )

    def snapshot(self) -> DisplayStatus:
        with self._lock:
            return DisplayStatus(
                mode=self._status.mode,
                detail=self._status.detail,
                started_at=self._status.started_at,
                duration_seconds=self._status.duration_seconds,
            )


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
    if "\x00" in filename:
        return None
    return filename


def fit_photo(path: Path, resolution: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        image.draft("RGB", resolution)
        image = ImageOps.exif_transpose(image)
        image = ImageOps.contain(image.convert("RGB"), resolution)
        canvas = Image.new("RGB", resolution, "white")
        canvas.paste(image, ((resolution[0] - image.width) // 2, (resolution[1] - image.height) // 2))
        return canvas


def trim_process_memory() -> None:
    gc.collect()
    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        return
    try:
        libc.malloc_trim(0)
    except AttributeError:
        return


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


def start_display_worker(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: Any | None = None,
    display_state: DisplayState | None = None,
) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: run_display_worker(photo_dir, config_store, photo_lock, weather_cache, display_state),
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
    display_state: DisplayState | None = None,
) -> None:
    while True:
        try:
            run_display_loop(photo_dir, config_store, photo_lock, weather_cache, display_state)
        except Exception:
            logger.exception("Display loop failed; retrying in 30 seconds")
            if display_state is not None:
                display_state.update("error", "Display loop failed; retrying", 30)
            time.sleep(30)


def run_display_loop(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: Any | None = None,
    display_state: DisplayState | None = None,
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
                try:
                    inky_display.set_image(image_for_display(image, inky_display.resolution))
                    inky_display.show()
                    if display_state is not None:
                        display_state.update("photo", current_image.name, config.photo_seconds)
                except Exception:
                    logger.exception("Display refresh failed while showing photo: {}", current_image)
                    raise
                finally:
                    del image
                    trim_process_memory()
            index += 1
            time.sleep(config.photo_seconds)
        else:
            logger.warning("No photos found in {}", photo_dir)
            if display_state is not None:
                display_state.update("idle", f"No photos found in {photo_dir}", None)

        config = config_store.load()
        target_resolution = oriented_resolution(inky_display.resolution, config.frame_orientation)
        logger.info("Displaying weather screen")
        snapshot = weather_cache.get(config) if weather_cache is not None else weather_client.fetch_or_cached(config)
        weather_image = render_weather_screen(target_resolution, config, snapshot)
        try:
            inky_display.set_image(image_for_display(weather_image, inky_display.resolution))
            inky_display.show()
            if display_state is not None:
                display_state.update("weather", config.location_name, config.weather_seconds)
        except Exception:
            logger.exception("Display refresh failed while showing weather screen")
            raise
        finally:
            del weather_image
            trim_process_memory()
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
        from inky_slideshow.admin import WeatherCache, run_admin_server

        weather_cache = WeatherCache()
        display_state = DisplayState() if mode == "combined" else None
        if mode == "combined":
            start_display_worker(photo_dir, config_store, photo_lock, weather_cache, display_state)
        run_admin_server(photo_dir, config_store, photo_lock, weather_cache, display_state)


if __name__ == "__main__":
    main()

import sys
import types
from datetime import datetime, timezone

import pytest
from PIL import Image

from inky_slideshow import slideshow
from inky_slideshow.slideshow import (
    AppConfig,
    ConfigStore,
    WeatherSnapshot,
    fit_photo,
    image_for_display,
    list_photos,
    managed_photo_path,
    oriented_resolution,
    parse_weather,
    render_weather_screen,
)


def test_config_store_creates_and_reloads_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    defaults = AppConfig(photo_seconds=12, weather_seconds=6)
    store = ConfigStore(config_path, defaults)

    assert store.load() == defaults
    assert config_path.exists()

    store.save(AppConfig(photo_seconds=20, weather_seconds=10, location_name="Paris"))
    loaded = store.load()

    assert loaded.photo_seconds == 20
    assert loaded.weather_seconds == 10
    assert loaded.location_name == "Paris"


def test_managed_photo_path_rejects_unsafe_names(tmp_path):
    assert managed_photo_path(tmp_path, "frame.jpg") == (tmp_path / "frame.jpg").resolve()
    assert managed_photo_path(tmp_path, "frame.heic") == (tmp_path / "frame.heic").resolve()

    with pytest.raises(ValueError):
        managed_photo_path(tmp_path, "../frame.jpg")

    with pytest.raises(ValueError):
        managed_photo_path(tmp_path, "notes.txt")


def test_list_photos_filters_allowed_extensions(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"")
    (tmp_path / "a.png").write_bytes(b"")
    (tmp_path / "c.heic").write_bytes(b"")
    (tmp_path / "d.heif").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")

    assert [path.name for path in list_photos(tmp_path)] == ["a.png", "b.jpg", "c.heic", "d.heif"]


def test_fit_photo_preserves_full_image_with_white_padding(tmp_path):
    photo_path = tmp_path / "wide.png"
    image = Image.new("RGB", (100, 50), "black")
    image.save(photo_path)

    fitted = fit_photo(photo_path, (100, 100))

    assert fitted.size == (100, 100)
    assert fitted.getpixel((50, 10)) == (255, 255, 255)
    assert fitted.getpixel((50, 25)) == (0, 0, 0)
    assert fitted.getpixel((50, 74)) == (0, 0, 0)
    assert fitted.getpixel((50, 90)) == (255, 255, 255)


def test_orientation_helpers_choose_frame_shape_and_native_display_size():
    assert oriented_resolution((480, 800), "horizontal") == (800, 480)
    assert oriented_resolution((480, 800), "vertical") == (480, 800)

    image = Image.new("RGB", (800, 480), "white")
    assert image_for_display(image, (480, 800)).size == (480, 800)


def test_render_weather_screen_returns_rgb_image():
    snapshot = WeatherSnapshot(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        location_name="London",
        temperature_c=10.4,
        feels_like_c=8.2,
        weather_code=0,
        wind_mph=6.1,
        uv_index=1.2,
        air_quality_index=2,
        sunrise="2026-04-13T06:07",
        sunset="2026-04-13T19:54",
        hourly=[{"time": "2026-04-13T18:00", "weather_code": 0}],
    )

    image = render_weather_screen((800, 480), AppConfig(), snapshot, now=datetime(2026, 4, 13, 17, 40, tzinfo=timezone.utc))

    assert image.size == (800, 480)
    assert image.mode == "RGB"



def test_parse_weather_uses_current_daily_hourly_and_air_quality():
    forecast = {
        "current": {
            "temperature_2m": 10,
            "apparent_temperature": 8,
            "weather_code": 0,
            "wind_speed_10m": 6,
        },
        "hourly": {
            "time": ["2000-01-01T00:00", "2999-01-01T01:00"],
            "temperature_2m": [9, 11],
            "weather_code": [1, 2],
            "uv_index": [1, 3],
        },
        "daily": {
            "sunrise": ["2026-04-13T06:07"],
            "sunset": ["2026-04-13T19:54"],
        },
    }
    air_quality = {
        "hourly": {
            "time": ["2000-01-01T00:00", "2999-01-01T01:00"],
            "european_aqi": [2, 22],
        }
    }

    snapshot = parse_weather("London", forecast, air_quality)

    assert snapshot.location_name == "London"
    assert snapshot.temperature_c == 10
    assert snapshot.feels_like_c == 8
    assert snapshot.wind_mph == 6
    assert snapshot.sunrise == "2026-04-13T06:07"
    assert snapshot.sunset == "2026-04-13T19:54"
    assert snapshot.hourly[0]["weather_code"] == 2


def test_rotate_photo_updates_image_dimensions(tmp_path):
    photo_path = tmp_path / "test.jpg"
    Image.new("RGB", (12, 8), "white").save(photo_path)

    slideshow.rotate_photo(photo_path, -90)

    with Image.open(photo_path) as rotated:
        assert rotated.size == (8, 12)


def test_display_worker_retries_after_failure(monkeypatch, tmp_path):
    calls = {"display": 0, "sleep": 0}

    def fake_display_loop(photo_dir, config_store):
        calls["display"] += 1
        if calls["display"] == 1:
            raise RuntimeError("display failed")
        raise KeyboardInterrupt

    def fake_sleep(seconds):
        calls["sleep"] += 1
        assert seconds == 30

    monkeypatch.setattr(slideshow, "run_display_loop", fake_display_loop)
    monkeypatch.setattr(slideshow.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        slideshow.run_display_worker(tmp_path, ConfigStore(tmp_path / "config.json", AppConfig()))

    assert calls == {"display": 2, "sleep": 1}


def test_display_loop_shows_weather_after_each_photo(monkeypatch, tmp_path):
    events = []

    class FakeDisplay:
        WHITE = 0
        resolution = (800, 480)

        def set_border(self, color):
            self.border = color

        def set_image(self, image):
            self.current_image = image

        def show(self):
            events.append(self.current_image)

    fake_display = FakeDisplay()
    fake_auto = types.ModuleType("inky.auto")
    fake_auto.auto = lambda ask_user=True: fake_display
    monkeypatch.setitem(sys.modules, "inky", types.ModuleType("inky"))
    monkeypatch.setitem(sys.modules, "inky.auto", fake_auto)

    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"not used")
    photo_image = Image.new("RGB", (800, 480), "black")
    weather_image = Image.new("RGB", (800, 480), "white")
    store = ConfigStore(tmp_path / "config.json", AppConfig(photo_seconds=7, weather_seconds=3))

    monkeypatch.setattr(slideshow, "list_photos", lambda photo_dir: [photo])
    monkeypatch.setattr(slideshow, "fit_photo", lambda path, resolution: photo_image)
    monkeypatch.setattr(slideshow, "render_weather_screen", lambda resolution, config, snapshot: weather_image)
    monkeypatch.setattr(slideshow.WeatherClient, "fetch_or_cached", lambda self, config: None)
    monkeypatch.setattr(slideshow.random, "randint", lambda start, stop: 0)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(slideshow.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        slideshow.run_display_loop(tmp_path, store)

    assert len(events) == 2
    assert events[0] is photo_image
    assert events[1] is weather_image
    assert sleeps == [7, 3]

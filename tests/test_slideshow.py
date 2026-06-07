import io
from datetime import datetime, timezone

import pytest
from PIL import Image

import inky_slideshow.slideshow as slideshow
from inky_slideshow.slideshow import (
    AppConfig,
    ConfigStore,
    WeatherSnapshot,
    create_app,
    fit_photo,
    list_photos,
    managed_photo_path,
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


def test_flask_routes_update_upload_and_delete(tmp_path):
    photo_dir = tmp_path / "photos"
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(photo_dir, store)
    client = app.test_client()

    response = client.post(
        "/settings",
        data={
            "photo_seconds": "5",
            "weather_seconds": "3",
            "location_name": "Paris",
            "latitude": "48.8566",
            "longitude": "2.3522",
        },
    )
    assert response.status_code == 302
    assert store.load().photo_seconds == 5
    assert store.load().location_name == "Paris"

    image = Image.new("RGB", (10, 10), "white")
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="JPEG")
    image_bytes.seek(0)

    response = client.post("/photos", data={"photo": (image_bytes, "test.jpg")}, content_type="multipart/form-data")
    assert response.status_code == 302
    assert (photo_dir / "test.jpg").exists()

    response = client.get("/photos/test.jpg")
    assert response.status_code == 200

    response = client.post("/photos/test.jpg/delete")
    assert response.status_code == 302
    assert not (photo_dir / "test.jpg").exists()


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

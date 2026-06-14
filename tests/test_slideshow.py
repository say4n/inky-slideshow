import sys
import types
from io import BytesIO
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from PIL import Image

from inky_slideshow import slideshow
from inky_slideshow import admin
from inky_slideshow.admin import create_app
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
    assert managed_photo_path(tmp_path, "20230619_072101~2.jpeg") == (
        tmp_path / "20230619_072101~2.jpeg"
    ).resolve()
    assert managed_photo_path(tmp_path, "family photo (edited)…v2.jpeg") == (
        tmp_path / "family photo (edited)…v2.jpeg"
    ).resolve()
    assert managed_photo_path(tmp_path, "scan..final.jpg") == (tmp_path / "scan..final.jpg").resolve()

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
        humidity_percent=60,
        uv_index=1.2,
        air_quality_index=2,
        today_low_c=8,
        today_high_c=14,
        tomorrow_low_c=7,
        tomorrow_high_c=13,
        tomorrow_weather_code=2,
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
            "relative_humidity_2m": 61,
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
            "temperature_2m_min": [7, 9],
            "temperature_2m_max": [14, 16],
            "weather_code": [0, 61],
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
    assert snapshot.humidity_percent == 61
    assert snapshot.today_low_c == 7
    assert snapshot.today_high_c == 14
    assert snapshot.tomorrow_low_c == 9
    assert snapshot.tomorrow_high_c == 16
    assert snapshot.tomorrow_weather_code == 61
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

    def fake_display_loop(photo_dir, config_store, photo_lock=None, weather_cache=None):
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


def test_admin_settings_updates_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig(photo_seconds=7, weather_seconds=3))
    app = create_app(tmp_path, store)

    response = app.test_client().post(
        "/settings",
        data={
            "photo_seconds": "22",
            "weather_seconds": "11",
            "location_name": "Paris",
            "latitude": "48.8566",
            "longitude": "2.3522",
            "frame_orientation": "vertical",
        },
    )

    assert response.status_code == 302
    loaded = store.load()
    assert loaded.photo_seconds == 22
    assert loaded.weather_seconds == 11
    assert loaded.location_name == "Paris"
    assert loaded.latitude == 48.8566
    assert loaded.longitude == 2.3522
    assert loaded.frame_orientation == "vertical"


def test_admin_upload_rejects_unsafe_and_oversized_files(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    client = create_app(tmp_path, store).test_client()

    unsafe = client.post(
        "/photos",
        data={"photo": (BytesIO(b"abc"), "../bad.jpg")},
        content_type="multipart/form-data",
    )
    oversized = create_app(tmp_path, store, upload_limit=8).test_client().post(
        "/photos",
        data={"photo": (BytesIO(b"0123456789" * 4), "big.jpg")},
        content_type="multipart/form-data",
    )

    assert unsafe.status_code == 302
    assert unsafe.location == "/?uploaded=0&failed=1"
    assert oversized.status_code == 413


def test_admin_default_upload_limit_supports_photo_batches(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)

    assert app.config["MAX_CONTENT_LENGTH"] == 256 * 1024 * 1024


def image_upload(color: str, filename: str) -> tuple[BytesIO, str]:
    image_bytes = BytesIO()
    Image.new("RGB", (4, 4), color).save(image_bytes, format="PNG")
    image_bytes.seek(0)
    return image_bytes, filename


def test_admin_upload_validates_and_saves_photo(monkeypatch, tmp_path):
    calls = []
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)
    image_bytes, filename = image_upload("white", "photo.png")

    def fake_validate(path):
        calls.append(path)

    monkeypatch.setattr(admin, "validate_image", fake_validate)

    response = app.test_client().post(
        "/photos",
        data={"photo": (image_bytes, filename)},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.location == "/?uploaded=1&failed=0"
    assert (tmp_path / "photo.png").exists()
    assert calls


def test_admin_upload_saves_multiple_photos(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)

    response = app.test_client().post(
        "/photos",
        data={
            "photo": [
                image_upload("white", "one.png"),
                image_upload("black", "two.png"),
            ]
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.location == "/?uploaded=2&failed=0"
    assert (tmp_path / "one.png").exists()
    assert (tmp_path / "two.png").exists()


def test_admin_upload_skips_invalid_files_in_batch(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)

    response = app.test_client().post(
        "/photos",
        data={
            "photo": [
                image_upload("white", "valid.png"),
                (BytesIO(b"not an image"), "broken.png"),
                (BytesIO(b"unsafe"), "../unsafe.png"),
            ]
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Uploaded 1 image; skipped 2 files." in response.data
    assert (tmp_path / "valid.png").exists()
    assert not (tmp_path / "broken.png").exists()
    assert not (tmp_path / "unsafe.png").exists()


def test_admin_upload_rejects_empty_upload(tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)

    response = app.test_client().post(
        "/photos",
        data={},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400


def test_admin_rotate_and_delete_photo(monkeypatch, tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    thumbnail_dir = tmp_path / ".thumbnails"
    thumbnail_dir.mkdir()
    stale_thumbnail = thumbnail_dir / "photo.jpg.123.5.jpg"
    stale_thumbnail.write_bytes(b"stale")
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    app = create_app(tmp_path, store)
    rotations = []

    def fake_rotate(path, degrees):
        rotations.append((path, degrees))

    monkeypatch.setattr(admin, "rotate_photo", fake_rotate)
    client = app.test_client()

    rotate_response = client.post("/photos/photo.jpg/rotate", data={"direction": "left"})
    delete_response = client.post("/photos/photo.jpg/delete")

    assert rotate_response.status_code == 302
    assert rotations == [(photo.resolve(), 90)]
    assert delete_response.status_code == 302
    assert not photo.exists()
    assert not stale_thumbnail.exists()


def test_admin_gallery_uses_thumbnail_urls(tmp_path):
    Image.new("RGB", (12, 8), "white").save(tmp_path / "photo.jpg")
    store = ConfigStore(tmp_path / "config.json", AppConfig())

    response = create_app(tmp_path, store).test_client().get("/")

    assert response.status_code == 200
    assert b"/photos/photo.jpg/thumbnail" in response.data
    assert b'src="/photos/photo.jpg"' not in response.data


def test_admin_thumbnail_route_creates_small_jpeg(tmp_path):
    Image.new("RGB", (1200, 800), "black").save(tmp_path / "large.jpg")
    store = ConfigStore(tmp_path / "config.json", AppConfig())

    response = create_app(tmp_path, store).test_client().get("/photos/large.jpg/thumbnail")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    thumbnails = list((tmp_path / ".thumbnails").glob("large.jpg.*.jpg"))
    assert len(thumbnails) == 1
    with Image.open(thumbnails[0]) as image:
        assert image.size == (320, 320)


def test_admin_thumbnail_route_accepts_tilde_filenames(tmp_path):
    filename = "family photo (edited)…20230619_072101~2.jpeg"
    Image.new("RGB", (1200, 800), "black").save(tmp_path / filename)
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    client = create_app(tmp_path, store).test_client()

    page_response = client.get("/")
    response = client.get("/photos/family%20photo%20%28edited%29%E2%80%A620230619_072101%7E2.jpeg/thumbnail")

    assert b"/photos/family%20photo%20%28edited%29%E2%80%A620230619_072101%7E2.jpeg/thumbnail" in page_response.data
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"


def test_admin_thumbnail_honors_exif_orientation(tmp_path):
    image = Image.new("RGB", (800, 400), "white")
    image.paste((0, 0, 0), (0, 0, 400, 400))
    exif = image.getexif()
    exif[274] = 6
    image.save(tmp_path / "oriented.jpg", exif=exif, quality=95)
    store = ConfigStore(tmp_path / "config.json", AppConfig())

    response = create_app(tmp_path, store).test_client().get("/photos/oriented.jpg/thumbnail")

    assert response.status_code == 200
    thumbnail_path = next((tmp_path / ".thumbnails").glob("oriented.jpg.*.jpg"))
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.size == (320, 320)
        assert thumbnail.getpixel((170, 20))[0] < 80
        assert thumbnail.getpixel((170, 300))[0] > 175


def test_admin_weather_preview_uses_cache(monkeypatch, tmp_path):
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    rendered = Image.new("RGB", (8, 8), "white")
    calls = {"cache": 0}

    class FakeCache:
        def get(self, config):
            calls["cache"] += 1
            return None

    monkeypatch.setattr(admin, "render_weather_screen", lambda resolution, config, snapshot: rendered)
    app = create_app(tmp_path, store, weather_cache=FakeCache())

    response = app.test_client().get("/weather-screen")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert calls["cache"] == 1


def test_admin_css_serves_from_working_directory(monkeypatch, tmp_path):
    css_dir = tmp_path / "admin" / "public"
    css_dir.mkdir(parents=True)
    (css_dir / "admin.css").write_text(".panel{display:block}\n")
    store = ConfigStore(tmp_path / "config.json", AppConfig())
    monkeypatch.chdir(tmp_path)

    response = create_app(tmp_path, store).test_client().get("/admin.css")

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    assert b".panel" in response.data


def test_weather_cache_reuses_fresh_snapshot(monkeypatch):
    snapshots = [
        WeatherSnapshot(
            fetched_at="now",
            location_name="London",
            temperature_c=1,
            feels_like_c=1,
            weather_code=0,
            wind_mph=1,
            humidity_percent=1,
            uv_index=1,
            air_quality_index=1,
            today_low_c=1,
            today_high_c=1,
            tomorrow_low_c=1,
            tomorrow_high_c=1,
            tomorrow_weather_code=0,
            sunrise=None,
            sunset=None,
            hourly=[],
        )
    ]
    cache = admin.WeatherCache(ttl_seconds=60)
    calls = []

    def fake_fetch(self, config):
        calls.append(config)
        return snapshots[0]

    monkeypatch.setattr(admin.WeatherClient, "fetch_or_cached", fake_fetch)

    assert cache.get(AppConfig()) is snapshots[0]
    assert cache.get(AppConfig()) is snapshots[0]
    assert len(calls) == 1


def test_cli_modes_branch_to_display_admin_or_combined(monkeypatch, tmp_path):
    events = []

    def fake_display_loop(photo_dir, config_store, photo_lock=None, weather_cache=None):
        events.append(("display", photo_dir))

    def fake_start_display_worker(photo_dir, config_store, photo_lock=None, weather_cache=None):
        events.append(("worker", photo_dir))

    def fake_run_admin_server(photo_dir, config_store, photo_lock=None, weather_cache=None):
        events.append(("admin", photo_dir))

    monkeypatch.setattr(slideshow, "run_display_loop", fake_display_loop)
    monkeypatch.setattr(slideshow, "start_display_worker", fake_start_display_worker)
    monkeypatch.setattr(admin, "run_admin_server", fake_run_admin_server)
    runner = CliRunner()

    display = runner.invoke(slideshow.main, [str(tmp_path), "--config", str(tmp_path / "display.json"), "--mode", "display"])
    admin_only = runner.invoke(slideshow.main, [str(tmp_path), "--config", str(tmp_path / "admin.json"), "--mode", "admin"])
    combined = runner.invoke(slideshow.main, [str(tmp_path), "--config", str(tmp_path / "combined.json"), "--mode", "combined"])

    assert display.exit_code == 0
    assert admin_only.exit_code == 0
    assert combined.exit_code == 0
    assert [event[0] for event in events] == ["display", "admin", "worker", "admin"]

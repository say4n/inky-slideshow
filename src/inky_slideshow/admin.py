from __future__ import annotations

import io
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, redirect, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

from .render_weather import render_weather_screen
from .slideshow import (
    ALLOWED_EXTENSIONS,
    AppConfig,
    ConfigStore,
    DisplayState,
    DisplayStatus,
    WeatherClient,
    WeatherSnapshot,
    list_photos,
    managed_photo_path,
    normalize_orientation,
    oriented_resolution,
    rotate_photo,
)

DEFAULT_UPLOAD_LIMIT = 256 * 1024 * 1024
INGEST_MAX_EDGE = 2400
WEATHER_CACHE_SECONDS = 15 * 60
REPO_ROOT = Path(__file__).resolve().parents[2]
THUMBNAIL_SIZE = (240, 240)


class WeatherCache:
    def __init__(self, ttl_seconds: int = WEATHER_CACHE_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._client = WeatherClient()
        self._lock = threading.RLock()
        self._snapshot: WeatherSnapshot | None = None
        self._fetched_at = 0.0

    def get(self, config: AppConfig) -> WeatherSnapshot | None:
        now = time.monotonic()
        with self._lock:
            if self._snapshot is not None and now - self._fetched_at < self.ttl_seconds:
                return self._snapshot
            snapshot = self._client.fetch_or_cached(config)
            if snapshot is not None:
                self._snapshot = snapshot
                self._fetched_at = now
            return snapshot


def create_app(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: WeatherCache | None = None,
    display_state: DisplayState | None = None,
    upload_limit: int = DEFAULT_UPLOAD_LIMIT,
) -> Flask:
    photo_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = photo_dir / ".uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir = photo_dir / ".thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    lock = photo_lock or threading.RLock()
    cache = weather_cache or WeatherCache()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = upload_limit

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(error: RequestEntityTooLarge) -> tuple[str, int]:
        return "Uploaded file is too large", 413

    @app.get("/")
    def index() -> str:
        with lock:
            photos = [path.name for path in list_photos(photo_dir)]
        uploaded = non_negative_int(request.args.get("uploaded"))
        failed = non_negative_int(request.args.get("failed"))
        status = display_state.snapshot() if display_state is not None else DisplayStatus(mode="admin")
        return render_page(config_store.load(), photos, status, uploaded=uploaded, failed=failed)

    @app.get("/admin.css")
    def admin_css() -> Response:
        css_path = Path.cwd() / "admin" / "public" / "admin.css"
        if not css_path.exists():
            css_path = REPO_ROOT / "admin" / "public" / "admin.css"
        return send_file(css_path, mimetype="text/css")

    @app.get("/weather-screen")
    def weather_screen() -> Response:
        config = config_store.load()
        snapshot = cache.get(config)
        image = render_weather_screen(oriented_resolution((800, 480), config.frame_orientation), config, snapshot)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return send_file(buffer, mimetype="image/png")

    @app.post("/settings")
    def settings() -> Response:
        current = config_store.load()
        config_store.save(
            AppConfig(
                photo_seconds=positive_int(request.form.get("photo_seconds"), current.photo_seconds),
                weather_seconds=positive_int(request.form.get("weather_seconds"), current.weather_seconds),
                host=current.host,
                port=current.port,
                location_name=(request.form.get("location_name") or current.location_name).strip()
                or current.location_name,
                latitude=float_value(request.form.get("latitude"), current.latitude),
                longitude=float_value(request.form.get("longitude"), current.longitude),
                frame_orientation=normalize_orientation(request.form.get("frame_orientation")),
            )
        )
        return redirect("/")

    @app.post("/photos")
    def upload_photo() -> Response | tuple[str, int]:
        uploaded_files = [uploaded for uploaded in request.files.getlist("photo") if uploaded.filename]
        if not uploaded_files:
            return "No photo uploaded", 400

        saved = 0
        failed = 0
        for uploaded in uploaded_files:
            try:
                target = uploaded_photo_target(photo_dir, uploaded.filename)
            except ValueError:
                failed += 1
                continue

            token = time.monotonic_ns()
            upload_path = upload_dir / f"{token}-{uploaded.filename}"
            processed_path = upload_dir / f"{token}-{target.name}"
            try:
                uploaded.save(upload_path)
                normalize_uploaded_photo(upload_path, processed_path)
                with lock:
                    processed_path.replace(target)
                saved += 1
            except Exception:
                failed += 1
            finally:
                upload_path.unlink(missing_ok=True)
                processed_path.unlink(missing_ok=True)

        return redirect(f"/?uploaded={saved}&failed={failed}")

    @app.get("/photos/<path:filename>")
    def photo(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            abort(404)
        if not target.exists():
            abort(404)
        return send_file(target)

    @app.get("/photos/<path:filename>/thumbnail")
    def thumbnail(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            abort(404)
        if not target.exists():
            abort(404)
        try:
            thumbnail_path = thumbnail_for_photo(target, thumbnail_dir, lock)
        except (OSError, UnidentifiedImageError):
            abort(404)
        return send_file(thumbnail_path, mimetype="image/jpeg")

    @app.post("/photos/<path:filename>/delete")
    def delete_photo(filename: str) -> Response:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            return redirect("/")
        with lock:
            target.unlink(missing_ok=True)
            cleanup_old_thumbnails(thumbnail_dir, target.name)
        return redirect("/")

    @app.post("/photos/<path:filename>/rotate")
    def rotate(filename: str) -> Response | tuple[str, int]:
        try:
            target = managed_photo_path(photo_dir, filename)
        except ValueError:
            return "Not found", 404
        if not target.exists():
            return "Not found", 404
        degrees = 90 if request.form.get("direction") == "left" else -90
        with lock:
            rotate_photo(target, degrees)
        return redirect("/")

    return app


def run_admin_server(
    photo_dir: Path,
    config_store: ConfigStore,
    photo_lock: threading.RLock | None = None,
    weather_cache: WeatherCache | None = None,
    display_state: DisplayState | None = None,
) -> None:
    config = config_store.load()
    app = create_app(
        photo_dir,
        config_store,
        photo_lock=photo_lock,
        weather_cache=weather_cache,
        display_state=display_state,
    )
    app.run(host=config.host, port=config.port, threaded=True, use_reloader=False)


def positive_int(value: str | None, fallback: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def non_negative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def uploaded_photo_target(photo_dir: Path, filename: str) -> Path:
    return managed_photo_path(photo_dir, filename).with_suffix(".jpg")


def normalize_uploaded_photo(source: Path, target: Path, max_edge: int = INGEST_MAX_EDGE) -> None:
    with Image.open(source) as image:
        image.draft("RGB", (max_edge, max_edge))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image.convert("RGB").save(target, format="JPEG", quality=88, optimize=True, progressive=True)


def float_value(value: str | None, fallback: float) -> float:
    try:
        return float(value or "")
    except ValueError:
        return fallback


def thumbnail_for_photo(
    photo_path: Path,
    thumbnail_dir: Path,
    photo_lock: threading.RLock,
    size: tuple[int, int] = THUMBNAIL_SIZE,
) -> Path:
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    stat = photo_path.stat()
    thumbnail_path = thumbnail_dir / f"{photo_path.name}.{stat.st_mtime_ns}.{stat.st_size}.jpg"
    if thumbnail_path.exists():
        return thumbnail_path

    with photo_lock:
        stat = photo_path.stat()
        thumbnail_path = thumbnail_dir / f"{photo_path.name}.{stat.st_mtime_ns}.{stat.st_size}.jpg"
        if thumbnail_path.exists():
            return thumbnail_path
        with Image.open(photo_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(size)
            thumbnail = Image.new("RGB", size, "white")
            image = image.convert("RGB")
            thumbnail.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
            thumbnail.save(thumbnail_path, format="JPEG", quality=82, optimize=True)
        cleanup_old_thumbnails(thumbnail_dir, photo_path.name, thumbnail_path)
        return thumbnail_path


def cleanup_old_thumbnails(thumbnail_dir: Path, photo_name: str, keep: Path | None = None) -> None:
    for path in thumbnail_dir.glob(f"{photo_name}.*.jpg"):
        if path != keep:
            path.unlink(missing_ok=True)


def escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def photo_url(filename: str) -> str:
    from urllib.parse import quote

    return quote(filename, safe="").replace("~", "%7E")


def render_page(
    config: AppConfig,
    photos: list[str],
    display_status: DisplayStatus,
    uploaded: int | None = None,
    failed: int | None = None,
) -> str:
    orientation = normalize_orientation(config.frame_orientation)
    photo_cards = "\n".join(render_photo_card(photo, orientation) for photo in photos)
    upload_message = render_upload_message(uploaded, failed)
    weather_cache_key = int(time.time())
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Inky Console</title>
    <link rel="stylesheet" href="/admin.css">
  </head>
  <body class="bg-stone-100 font-sans text-stone-950 antialiased">
    <main class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <header class="mb-7">
        <h1 class="text-4xl font-black tracking-normal">Inky Console</h1>
        <p class="mt-2 text-sm text-stone-600">Manage the slideshow, frame orientation, weather, and uploaded photos.</p>
      </header>

      <div class="grid gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)]">
        <div class="grid gap-6">
          <section class="panel">
            <h2 class="mb-5 text-lg font-black">Display Settings</h2>
            <form class="grid gap-4 sm:grid-cols-2" action="/settings" method="post">
              <label class="field-label">Photo seconds <input class="field-input" name="photo_seconds" type="number" min="1" value="{escape_html(config.photo_seconds)}"></label>
              <label class="field-label">Weather seconds <input class="field-input" name="weather_seconds" type="number" min="1" value="{escape_html(config.weather_seconds)}"></label>
              <div class="field-label sm:col-span-2">Frame orientation
                <div class="grid grid-cols-2 gap-2">
                  <label><input class="peer sr-only" name="frame_orientation" type="radio" value="horizontal" {"checked" if orientation == "horizontal" else ""}><span class="flex min-h-11 cursor-pointer items-center justify-center rounded-lg border border-stone-300 bg-white px-3 font-bold text-stone-950 peer-checked:border-stone-950 peer-checked:bg-stone-950 peer-checked:text-white">Landscape</span></label>
                  <label><input class="peer sr-only" name="frame_orientation" type="radio" value="vertical" {"checked" if orientation == "vertical" else ""}><span class="flex min-h-11 cursor-pointer items-center justify-center rounded-lg border border-stone-300 bg-white px-3 font-bold text-stone-950 peer-checked:border-stone-950 peer-checked:bg-stone-950 peer-checked:text-white">Portrait</span></label>
                </div>
              </div>
              <label class="field-label">Weather city <input class="field-input" name="location_name" value="{escape_html(config.location_name)}"></label>
              <label class="field-label">Latitude <input class="field-input" name="latitude" type="number" step="0.0001" value="{escape_html(config.latitude)}"></label>
              <label class="field-label">Longitude <input class="field-input" name="longitude" type="number" step="0.0001" value="{escape_html(config.longitude)}"></label>
              <div class="flex flex-wrap items-center gap-3 sm:col-span-2">
                <button class="btn" type="submit">Save settings</button>
                <span class="text-sm text-stone-600">Photos display for {escape_html(config.photo_seconds)}s, then weather for {escape_html(config.weather_seconds)}s.</span>
              </div>
            </form>
          </section>

          <section class="panel">
            <h2 class="mb-5 text-lg font-black">Upload Images</h2>
            <form class="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end" action="/photos" method="post" enctype="multipart/form-data">
              <label class="field-label">Image files <input class="field-input" name="photo" type="file" accept=".png,.jpg,.jpeg,.heic,.heif,image/png,image/jpeg,image/heic,image/heif" multiple required></label>
              <button class="btn" type="submit">Upload</button>
            </form>
            {upload_message}
          </section>
        </div>

        <section class="panel">
          <h2 class="mb-5 text-lg font-black">Weather Preview</h2>
          <div class="rounded-lg border border-stone-300 bg-stone-200 p-4">
            <div class="mx-auto overflow-hidden border-[10px] border-stone-950 bg-white {"aspect-[3/5] max-w-80" if orientation == "vertical" else "aspect-[5/3] max-w-xl"}">
              <img class="h-full w-full object-contain" src="/weather-screen?cache={weather_cache_key}" alt="Weather screen preview">
            </div>
          </div>
          <p class="mt-3 text-sm text-stone-600">This preview uses the same Python renderer as the e-ink frame.</p>
          {render_display_status(display_status)}
        </section>
      </div>

      <section class="panel mt-6">
        <div class="mb-5 flex items-baseline justify-between gap-4">
          <h2 class="text-lg font-black">Photo Gallery</h2>
          <p class="text-sm text-stone-600">{len(photos)} images</p>
        </div>
        {f'<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{photo_cards}</div>' if photos else '<div class="rounded-lg border border-dashed border-stone-300 p-10 text-center text-sm text-stone-600">No photos uploaded yet.</div>'}
      </section>
    </main>
  </body>
</html>"""


def render_display_status(status: DisplayStatus) -> str:
    label = display_mode_label(status.mode)
    detail = status.detail or display_mode_detail(status.mode)
    started = display_started_label(status.started_at)
    duration = display_duration_label(status.duration_seconds)
    duration_row = (
        f"""
              <div>
                <dt class="text-xs font-bold uppercase text-stone-500">Duration</dt>
                <dd class="mt-1 font-bold text-stone-950">{escape_html(duration)}</dd>
              </div>"""
        if duration
        else ""
    )
    return f"""
          <section class="mt-5 rounded-lg border border-stone-300 bg-white p-4">
            <div class="flex items-start justify-between gap-4">
              <div>
                <h3 class="text-sm font-black uppercase text-stone-500">Display State</h3>
                <p class="mt-1 text-2xl font-black text-stone-950">{escape_html(label)}</p>
              </div>
              <span class="rounded-lg border border-stone-300 px-3 py-1 text-xs font-bold uppercase text-stone-600">{escape_html(status.mode)}</span>
            </div>
            <dl class="mt-4 grid gap-3 sm:grid-cols-2">
              <div>
                <dt class="text-xs font-bold uppercase text-stone-500">Current</dt>
                <dd class="mt-1 break-words font-bold text-stone-950">{escape_html(detail)}</dd>
              </div>
              <div>
                <dt class="text-xs font-bold uppercase text-stone-500">Since</dt>
                <dd class="mt-1 font-bold text-stone-950">{escape_html(started)}</dd>
              </div>{duration_row}
            </dl>
          </section>"""


def display_mode_label(mode: str) -> str:
    return {
        "photo": "Showing Photo",
        "weather": "Showing Weather",
        "idle": "Idle",
        "error": "Display Error",
        "admin": "Admin Only",
        "starting": "Starting",
    }.get(mode, mode.replace("_", " ").title())


def display_mode_detail(mode: str) -> str:
    return {
        "admin": "Display worker is not running in admin-only mode.",
        "starting": "Waiting for the first display refresh.",
    }.get(mode, "No display details available.")


def display_started_label(value: str | None) -> str:
    if not value:
        return "Not yet"
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return value
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def display_duration_label(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value} second{'s' if value != 1 else ''}"


def render_upload_message(uploaded: int | None, failed: int | None) -> str:
    if uploaded is None and failed is None:
        return ""
    uploaded_count = uploaded or 0
    failed_count = failed or 0
    if uploaded_count and failed_count:
        uploaded_label = "images" if uploaded_count != 1 else "image"
        failed_label = "files" if failed_count != 1 else "file"
        message = f"Uploaded {uploaded_count} {uploaded_label}; skipped {failed_count} {failed_label}."
    elif uploaded_count:
        message = f"Uploaded {uploaded_count} image{'s' if uploaded_count != 1 else ''}."
    elif failed_count:
        message = f"Skipped {failed_count} file{'s' if failed_count != 1 else ''}."
    else:
        message = "No images were uploaded."
    return f'<p class="mt-3 rounded-lg border border-stone-300 bg-white p-3 text-sm text-stone-600">{escape_html(message)}</p>'


def render_photo_card(photo: str, orientation: str) -> str:
    encoded = photo_url(photo)
    preview_shape = "aspect-[3/5]" if orientation == "vertical" else "aspect-[5/3]"
    return f"""
        <article class="rounded-lg border border-stone-300 bg-white p-3">
          <div class="flex items-center justify-center overflow-hidden border border-stone-300 bg-white {preview_shape}">
            <img class="h-full w-full object-contain" src="/photos/{encoded}/thumbnail" alt="{escape_html(photo)}" loading="lazy">
          </div>
          <p class="my-3 truncate text-xs font-bold text-stone-600" title="{escape_html(photo)}">{escape_html(photo)}</p>
          <div class="grid grid-cols-[1fr_1fr_1.3fr] gap-2">
            <form action="/photos/{encoded}/rotate" method="post"><input type="hidden" name="direction" value="left"><button class="btn btn-secondary w-full" type="submit">Left</button></form>
            <form action="/photos/{encoded}/rotate" method="post"><input type="hidden" name="direction" value="right"><button class="btn btn-secondary w-full" type="submit">Right</button></form>
            <form action="/photos/{encoded}/delete" method="post"><button class="btn btn-danger w-full" type="submit">Delete</button></form>
          </div>
        </article>"""

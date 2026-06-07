import childProcess from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import util from "node:util";

import express from "express";
import multer from "multer";

const execFile = util.promisify(childProcess.execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const allowedExtensions = new Set([".png", ".jpg", ".jpeg", ".heic", ".heif"]);
const defaultConfigPath = path.join(process.env.HOME || ".", ".config", "inky-slideshow", "config.json");
const baseDefaultConfig = {
  photo_seconds: 60,
  weather_seconds: 30,
  host: "0.0.0.0",
  port: 8080,
  location_name: "London",
  latitude: 51.5072,
  longitude: -0.1276,
  frame_orientation: "horizontal"
};

function parseArgs(argv) {
  const values = {
    photoDir: path.join(process.env.HOME || ".", "images"),
    config: defaultConfigPath,
    host: "0.0.0.0",
    port: 8080,
    defaults: { ...baseDefaultConfig },
    python: fs.existsSync(path.join(repoRoot, ".venv", "bin", "python"))
      ? path.join(repoRoot, ".venv", "bin", "python")
      : "python3"
  };

  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key.startsWith("--") || value === undefined) {
      continue;
    }
    i += 1;
    if (key === "--photo-dir") values.photoDir = value;
    if (key === "--config") values.config = value;
    if (key === "--host") values.host = value;
    if (key === "--port") values.port = Number(value);
    if (key === "--python") values.python = value;
    if (key === "--photo-seconds") values.defaults.photo_seconds = positiveInt(value, values.defaults.photo_seconds);
    if (key === "--weather-seconds") values.defaults.weather_seconds = positiveInt(value, values.defaults.weather_seconds);
    if (key === "--location-name") values.defaults.location_name = value;
    if (key === "--latitude") values.defaults.latitude = floatValue(value, values.defaults.latitude);
    if (key === "--longitude") values.defaults.longitude = floatValue(value, values.defaults.longitude);
    if (key === "--frame-orientation") values.defaults.frame_orientation = normalizeOrientation(value);
  }

  return values;
}

const options = parseArgs(process.argv.slice(2));
fs.mkdirSync(options.photoDir, { recursive: true });
fs.mkdirSync(path.dirname(options.config), { recursive: true });
fs.mkdirSync(path.join(options.photoDir, ".uploads"), { recursive: true });

const upload = multer({
  dest: path.join(options.photoDir, ".uploads"),
  limits: { fileSize: 100 * 1024 * 1024 }
});

function normalizeOrientation(value) {
  return value === "vertical" ? "vertical" : "horizontal";
}

function readConfig() {
  if (!fs.existsSync(options.config)) {
    writeConfig(options.defaults);
    return { ...options.defaults };
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(options.config, "utf8"));
    return {
      ...options.defaults,
      ...parsed,
      frame_orientation: normalizeOrientation(parsed.frame_orientation)
    };
  } catch {
    return { ...options.defaults };
  }
}

function writeConfig(config) {
  fs.mkdirSync(path.dirname(options.config), { recursive: true });
  fs.writeFileSync(options.config, `${JSON.stringify(config, null, 2)}\n`);
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function floatValue(value, fallback) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function safeName(filename) {
  const base = path.basename(filename || "");
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, "_");
  if (!cleaned || cleaned !== base || !allowedExtensions.has(path.extname(cleaned).toLowerCase())) {
    return null;
  }
  return cleaned;
}

function photoPath(filename) {
  const cleaned = safeName(filename);
  if (!cleaned) return null;
  const fullPath = path.resolve(options.photoDir, cleaned);
  return path.dirname(fullPath) === path.resolve(options.photoDir) ? fullPath : null;
}

function listPhotos() {
  return fs
    .readdirSync(options.photoDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && allowedExtensions.has(path.extname(entry.name).toLowerCase()))
    .map((entry) => entry.name)
    .sort((a, b) => a.localeCompare(b));
}

async function runPython(args, extraOptions = {}) {
  const env = {
    ...process.env,
    PYTHONPATH: [path.join(repoRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
  };
  return execFile(options.python, args, {
    cwd: repoRoot,
    env,
    maxBuffer: 25 * 1024 * 1024,
    ...extraOptions
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderPage(config, photos) {
  const orientation = normalizeOrientation(config.frame_orientation);
  const photoCards = photos
    .map(
      (photo) => `
        <article class="rounded-lg border border-stone-300 bg-white p-3">
          <div class="flex items-center justify-center overflow-hidden border border-stone-300 bg-white ${orientation === "vertical" ? "aspect-[3/5]" : "aspect-[5/3]"}">
            <img class="h-full w-full object-contain" src="/photos/${encodeURIComponent(photo)}" alt="${escapeHtml(photo)}">
          </div>
          <p class="my-3 truncate text-xs font-bold text-stone-600" title="${escapeHtml(photo)}">${escapeHtml(photo)}</p>
          <div class="grid grid-cols-[1fr_1fr_1.3fr] gap-2">
            <form action="/photos/${encodeURIComponent(photo)}/rotate" method="post"><input type="hidden" name="direction" value="left"><button class="btn btn-secondary w-full" type="submit">Left</button></form>
            <form action="/photos/${encodeURIComponent(photo)}/rotate" method="post"><input type="hidden" name="direction" value="right"><button class="btn btn-secondary w-full" type="submit">Right</button></form>
            <form action="/photos/${encodeURIComponent(photo)}/delete" method="post"><button class="btn btn-danger w-full" type="submit">Delete</button></form>
          </div>
        </article>`
    )
    .join("");

  return `<!doctype html>
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
              <label class="field-label">Photo seconds <input class="field-input" name="photo_seconds" type="number" min="1" value="${escapeHtml(config.photo_seconds)}"></label>
              <label class="field-label">Weather seconds <input class="field-input" name="weather_seconds" type="number" min="1" value="${escapeHtml(config.weather_seconds)}"></label>
              <div class="field-label sm:col-span-2">Frame orientation
                <div class="grid grid-cols-2 gap-2">
                  <label><input class="peer sr-only" name="frame_orientation" type="radio" value="horizontal" ${orientation === "horizontal" ? "checked" : ""}><span class="flex min-h-11 cursor-pointer items-center justify-center rounded-lg border border-stone-300 bg-white px-3 font-bold text-stone-950 peer-checked:border-stone-950 peer-checked:bg-stone-950 peer-checked:text-white">Landscape</span></label>
                  <label><input class="peer sr-only" name="frame_orientation" type="radio" value="vertical" ${orientation === "vertical" ? "checked" : ""}><span class="flex min-h-11 cursor-pointer items-center justify-center rounded-lg border border-stone-300 bg-white px-3 font-bold text-stone-950 peer-checked:border-stone-950 peer-checked:bg-stone-950 peer-checked:text-white">Portrait</span></label>
                </div>
              </div>
              <label class="field-label">Weather city <input class="field-input" name="location_name" value="${escapeHtml(config.location_name)}"></label>
              <label class="field-label">Latitude <input class="field-input" name="latitude" type="number" step="0.0001" value="${escapeHtml(config.latitude)}"></label>
              <label class="field-label">Longitude <input class="field-input" name="longitude" type="number" step="0.0001" value="${escapeHtml(config.longitude)}"></label>
              <div class="flex flex-wrap items-center gap-3 sm:col-span-2">
                <button class="btn" type="submit">Save settings</button>
                <span class="text-sm text-stone-600">Photos display for ${escapeHtml(config.photo_seconds)}s, then weather for ${escapeHtml(config.weather_seconds)}s.</span>
              </div>
            </form>
          </section>

          <section class="panel">
            <h2 class="mb-5 text-lg font-black">Upload Photo</h2>
            <form class="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end" action="/photos" method="post" enctype="multipart/form-data">
              <label class="field-label">Image file <input class="field-input" name="photo" type="file" accept=".png,.jpg,.jpeg,.heic,.heif,image/png,image/jpeg,image/heic,image/heif" required></label>
              <button class="btn" type="submit">Upload</button>
            </form>
          </section>
        </div>

        <section class="panel">
          <h2 class="mb-5 text-lg font-black">Weather Preview</h2>
          <div class="rounded-lg border border-stone-300 bg-stone-200 p-4">
            <div class="mx-auto overflow-hidden border-[10px] border-stone-950 bg-white ${orientation === "vertical" ? "aspect-[3/5] max-w-80" : "aspect-[5/3] max-w-xl"}">
              <img class="h-full w-full object-contain" src="/weather-screen?cache=${crypto.randomUUID()}" alt="Weather screen preview">
            </div>
          </div>
          <p class="mt-3 text-sm text-stone-600">This preview uses the same Python renderer as the e-ink frame.</p>
        </section>
      </div>

      <section class="panel mt-6">
        <div class="mb-5 flex items-baseline justify-between gap-4">
          <h2 class="text-lg font-black">Photo Gallery</h2>
          <p class="text-sm text-stone-600">${photos.length} images</p>
        </div>
        ${
          photos.length
            ? `<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">${photoCards}</div>`
            : `<div class="rounded-lg border border-dashed border-stone-300 p-10 text-center text-sm text-stone-600">No photos uploaded yet.</div>`
        }
      </section>
    </main>
  </body>
</html>`;
}

const app = express();
app.use(express.urlencoded({ extended: false }));
app.use("/assets/fonts", express.static(path.join(repoRoot, "src", "inky_slideshow", "assets", "fonts")));

app.get("/admin.css", (request, response) => {
  response.sendFile(path.join(repoRoot, "admin", "public", "admin.css"));
});

app.get("/", (request, response) => {
  response.send(renderPage(readConfig(), listPhotos()));
});

app.get("/weather-screen", async (request, response) => {
  try {
    const { stdout } = await runPython(["-m", "inky_slideshow.render_weather", "--config", options.config], {
      encoding: "buffer"
    });
    response.type("png").send(stdout);
  } catch (error) {
    response.status(502).send(error.stderr?.toString() || "Weather preview failed");
  }
});

app.post("/settings", (request, response) => {
  const current = readConfig();
  writeConfig({
    ...current,
    photo_seconds: positiveInt(request.body.photo_seconds, current.photo_seconds),
    weather_seconds: positiveInt(request.body.weather_seconds, current.weather_seconds),
    location_name: (request.body.location_name || current.location_name).trim() || current.location_name,
    latitude: floatValue(request.body.latitude, current.latitude),
    longitude: floatValue(request.body.longitude, current.longitude),
    frame_orientation: normalizeOrientation(request.body.frame_orientation)
  });
  response.redirect("/");
});

app.post("/photos", upload.single("photo"), async (request, response, next) => {
  if (!request.file) {
    response.status(400).send("No photo uploaded");
    return;
  }
  const name = safeName(request.file.originalname);
  if (!name) {
    fs.rmSync(request.file.path, { force: true });
    response.status(400).send("Unsupported or unsafe filename");
    return;
  }
  const target = photoPath(name);
  try {
    await runPython(["-m", "inky_slideshow.photo_tool", "validate", request.file.path]);
    fs.renameSync(request.file.path, target);
    response.redirect("/");
  } catch (error) {
    fs.rmSync(request.file.path, { force: true });
    next(error);
  }
});

app.get("/photos/:filename", (request, response) => {
  const target = photoPath(request.params.filename);
  if (!target || !fs.existsSync(target)) {
    response.status(404).send("Not found");
    return;
  }
  response.sendFile(target);
});

app.post("/photos/:filename/delete", (request, response) => {
  const target = photoPath(request.params.filename);
  if (target && fs.existsSync(target)) {
    fs.rmSync(target);
  }
  response.redirect("/");
});

app.post("/photos/:filename/rotate", async (request, response, next) => {
  const target = photoPath(request.params.filename);
  const direction = request.body.direction === "left" ? "left" : "right";
  if (!target || !fs.existsSync(target)) {
    response.status(404).send("Not found");
    return;
  }
  try {
    await runPython(["-m", "inky_slideshow.photo_tool", "rotate", target, direction]);
    response.redirect("/");
  } catch (error) {
    next(error);
  }
});

app.use((error, request, response, next) => {
  console.error(error);
  response.status(500).send("Admin action failed");
});

app.listen(options.port, options.host, () => {
  console.log(`Admin UI listening on http://${options.host}:${options.port}`);
});

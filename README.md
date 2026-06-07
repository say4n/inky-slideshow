# inky-slideshow

slideshow of images for inky impression (PIM 773)

![inky frame with a picture inside a photo frame](assets/demo.jpg)

-----

## Table of Contents

- [Installation](#installation)
- [License](#license)

## Installation

```console
pip install git+ssh://git@github.com/say4n/inky-slideshow
```

On the frame, the intended setup path is the installer script. It clones or
updates the repo, installs Node dependencies for the admin app, creates a local
Python virtualenv for the display service, writes the systemd units, enables
them, and starts the services.

```console
curl -fsSL https://raw.githubusercontent.com/say4n/inky-slideshow/main/scripts/install.sh | bash
```

You can override installer defaults with environment variables:

```console
curl -fsSL https://raw.githubusercontent.com/say4n/inky-slideshow/main/scripts/install.sh | \
  INKY_PHOTO_DIR=/home/sayan/images \
  INKY_WEB_PORT=8080 \
  INKY_FRAME_ORIENTATION=horizontal \
  INKY_PHOTO_SECONDS=60 \
  INKY_WEATHER_SECONDS=30 \
  bash
```

Supported installer variables include `INKY_INSTALL_DIR`, `INKY_PHOTO_DIR`,
`INKY_CONFIG_PATH`, `INKY_SERVICE_USER`, `INKY_SERVICE_NAME`, `INKY_REPO_URL`,
`INKY_REPO_REF`, `INKY_WEB_HOST`, `INKY_WEB_PORT`, `INKY_PHOTO_SECONDS`,
`INKY_WEATHER_SECONDS`, `INKY_FRAME_ORIENTATION`, `INKY_LOCATION_NAME`,
`INKY_LATITUDE`, and `INKY_LONGITUDE`.

## Usage

The slideshow runs as two systemd services: a Python display loop for the Inky
hardware and a Node.js LAN admin app. This keeps settings and photo management
available even if the display hardware path fails or restarts.

```console
hatch run slideshow /home/sayan/images
```

The admin UI listens on `http://<frame-host>:8080` by default. Use it to upload,
rotate, or delete photos, change the photo/weather durations, adjust the weather
location, and preview the weather screen. Supported photo formats are JPEG, PNG,
HEIC, and HEIF.

Default timings are 60 seconds for each photo and 30 seconds for the weather
screen:

```console
hatch run slideshow /home/sayan/images --photo-seconds 60 --weather-seconds 30
```

Settings are persisted to `~/.config/inky-slideshow/config.json` unless
`--config` is provided. The installer writes and starts
`inky-slideshow-web.service` and `inky-slideshow-display.service`; the included
unit files show the generated unit shape.

## License

`inky-slideshow` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.

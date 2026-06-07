#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${INKY_REPO_URL:-https://github.com/say4n/inky-slideshow.git}"
REPO_REF="${INKY_REPO_REF:-main}"
SERVICE_NAME="${INKY_SERVICE_NAME:-inky-slideshow}"
SERVICE_USER="${INKY_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
INSTALLER_REEXEC="${INKY_INSTALLER_REEXEC:-0}"

if [[ -z "${SERVICE_HOME}" ]]; then
  echo "Could not determine home directory for ${SERVICE_USER}" >&2
  exit 1
fi

INSTALL_DIR="${INKY_INSTALL_DIR:-${SERVICE_HOME}/inky-slideshow}"
PHOTO_DIR="${INKY_PHOTO_DIR:-${SERVICE_HOME}/images}"
CONFIG_PATH="${INKY_CONFIG_PATH:-${SERVICE_HOME}/.config/inky-slideshow/config.json}"
PHOTO_SECONDS="${INKY_PHOTO_SECONDS:-60}"
WEATHER_SECONDS="${INKY_WEATHER_SECONDS:-30}"
WEB_HOST="${INKY_WEB_HOST:-0.0.0.0}"
WEB_PORT="${INKY_WEB_PORT:-8080}"
LOCATION_NAME="${INKY_LOCATION_NAME:-London}"
LATITUDE="${INKY_LATITUDE:-51.5072}"
LONGITUDE="${INKY_LONGITUDE:--0.1276}"
FRAME_ORIENTATION="${INKY_FRAME_ORIENTATION:-horizontal}"
LEGACY_UNIT="${SERVICE_NAME}.service"
WEB_UNIT="${SERVICE_NAME}-web.service"
DISPLAY_UNIT="${SERVICE_NAME}-display.service"
WEB_UNIT_PATH="/etc/systemd/system/${WEB_UNIT}"
DISPLAY_UNIT_PATH="/etc/systemd/system/${DISPLAY_UNIT}"
REBOOT_REQUIRED=0

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer only supports Linux systemd hosts." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl was not found. This installer expects a systemd host." >&2
  exit 1
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required when the installer is not run as root." >&2
    exit 1
  fi
  sudo -v
  SUDO=(sudo)
fi

run_as_service_user() {
  if [[ "${EUID}" -eq 0 && "${SERVICE_USER}" != "root" ]]; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "${SERVICE_USER}" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
      sudo -H -u "${SERVICE_USER}" "$@"
    else
      echo "Need runuser or sudo to run commands as ${SERVICE_USER}." >&2
      exit 1
    fi
  else
    "$@"
  fi
}

install_os_packages() {
  local missing=()
  local python_dev_package="python3-dev"
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v node >/dev/null 2>&1 || missing+=(nodejs)
  command -v npm >/dev/null 2>&1 || missing+=(npm)
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  python3 -m venv --help >/dev/null 2>&1 || missing+=(python3-venv)

  if command -v python3 >/dev/null 2>&1; then
    python_dev_package="$(python3 - <<'PY'
import sys

print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")
PY
)"
  fi

  if command -v apt-get >/dev/null 2>&1; then
    local apt_packages=(
      build-essential
      git
      nodejs
      npm
      python3
      python3-dev
      python3-pip
      python3-venv
    )
    if apt-cache show "${python_dev_package}" >/dev/null 2>&1; then
      apt_packages+=("${python_dev_package}")
    fi
    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y "${apt_packages[@]}"
    return
  fi

  if [[ "${#missing[@]}" -eq 0 ]] && python3 - <<'PY'
import pathlib
import sysconfig

include_dir = pathlib.Path(sysconfig.get_paths()["include"])
raise SystemExit(0 if (include_dir / "Python.h").exists() else 1)
PY
  then
    return
  fi

  echo "Missing required install prerequisites." >&2
  echo "Install git, nodejs, npm, python3, python3-venv, python3-pip, python3-dev, and build-essential, then rerun this installer." >&2
  exit 1
}

install_os_packages

configure_hardware_access() {
  for group in spi gpio i2c; do
    if getent group "${group}" >/dev/null 2>&1; then
      "${SUDO[@]}" usermod -a -G "${group}" "${SERVICE_USER}"
    fi
  done

  if command -v raspi-config >/dev/null 2>&1; then
    if [[ ! -e /dev/spidev0.0 ]]; then
      "${SUDO[@]}" raspi-config nonint do_spi 0 || true
      REBOOT_REQUIRED=1
    fi
    "${SUDO[@]}" raspi-config nonint do_i2c 0 || true
  fi

  if [[ ! -e /dev/spidev0.0 ]]; then
    echo "Warning: /dev/spidev0.0 is not present. SPI may need a reboot before the display service can start." >&2
    REBOOT_REQUIRED=1
  fi
}

configure_hardware_access

run_as_service_user mkdir -p "${INSTALL_DIR}" "${PHOTO_DIR}" "$(dirname "${CONFIG_PATH}")"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  run_as_service_user git -C "${INSTALL_DIR}" fetch --tags origin
  run_as_service_user git -C "${INSTALL_DIR}" checkout "${REPO_REF}"
  run_as_service_user git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_REF}"
else
  rmdir "${INSTALL_DIR}" 2>/dev/null || true
  run_as_service_user git clone --branch "${REPO_REF}" "${REPO_URL}" "${INSTALL_DIR}"
fi

if [[ "${INSTALLER_REEXEC}" != "1" ]]; then
  export INKY_INSTALLER_REEXEC=1
  exec bash "${INSTALL_DIR}/scripts/install.sh"
fi

run_as_service_user python3 -m venv "${INSTALL_DIR}/.venv"
run_as_service_user "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
run_as_service_user "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade "${INSTALL_DIR}"
run_as_service_user npm --prefix "${INSTALL_DIR}" ci --omit=dev


TMP_WEB_UNIT="$(mktemp)"
TMP_DISPLAY_UNIT="$(mktemp)"

cat >"${TMP_WEB_UNIT}" <<UNIT
[Unit]
Description=Inky Slideshow Web Admin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/env node ${INSTALL_DIR}/admin/server.js --photo-dir "${PHOTO_DIR}" --config "${CONFIG_PATH}" --host ${WEB_HOST} --port ${WEB_PORT} --python ${INSTALL_DIR}/.venv/bin/python --photo-seconds ${PHOTO_SECONDS} --weather-seconds ${WEATHER_SECONDS} --location-name "${LOCATION_NAME}" --latitude ${LATITUDE} --longitude ${LONGITUDE} --frame-orientation ${FRAME_ORIENTATION}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

cat >"${TMP_DISPLAY_UNIT}" <<UNIT
[Unit]
Description=Inky Slideshow Display
After=network-online.target ${WEB_UNIT}
Wants=network-online.target ${WEB_UNIT}

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/src/inky_slideshow/slideshow.py "${PHOTO_DIR}" --mode display --config "${CONFIG_PATH}" --photo-seconds ${PHOTO_SECONDS} --weather-seconds ${WEATHER_SECONDS} --host ${WEB_HOST} --port ${WEB_PORT} --location-name "${LOCATION_NAME}" --latitude ${LATITUDE} --longitude ${LONGITUDE} --frame-orientation ${FRAME_ORIENTATION}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

"${SUDO[@]}" install -m 0644 "${TMP_WEB_UNIT}" "${WEB_UNIT_PATH}"
"${SUDO[@]}" install -m 0644 "${TMP_DISPLAY_UNIT}" "${DISPLAY_UNIT_PATH}"
rm -f "${TMP_WEB_UNIT}" "${TMP_DISPLAY_UNIT}"

"${SUDO[@]}" systemctl stop "${LEGACY_UNIT}" 2>/dev/null || true
"${SUDO[@]}" systemctl disable "${LEGACY_UNIT}" 2>/dev/null || true
"${SUDO[@]}" rm -f "/etc/systemd/system/${LEGACY_UNIT}"

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable "${WEB_UNIT}" "${DISPLAY_UNIT}"
"${SUDO[@]}" systemctl restart "${WEB_UNIT}" "${DISPLAY_UNIT}"

cat <<EOF
Installed ${SERVICE_NAME}.

Services:    ${WEB_UNIT}
             ${DISPLAY_UNIT}
Install dir: ${INSTALL_DIR}
Photo dir:   ${PHOTO_DIR}
Config:      ${CONFIG_PATH}
Admin UI:    http://<frame-host>:${WEB_PORT}

Useful commands:
  sudo systemctl status ${WEB_UNIT} ${DISPLAY_UNIT}
  sudo journalctl -u ${WEB_UNIT} -f
  sudo journalctl -u ${DISPLAY_UNIT} -f
EOF

if [[ "${REBOOT_REQUIRED}" == "1" ]]; then
  cat <<EOF

SPI was enabled or is still not visible. Reboot the frame if the display service
continues to report that /dev/spidev0.0 is missing:

  sudo reboot
EOF
fi

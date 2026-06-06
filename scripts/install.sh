#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${INKY_REPO_URL:-https://github.com/say4n/inky-slideshow.git}"
REPO_REF="${INKY_REPO_REF:-main}"
SERVICE_NAME="${INKY_SERVICE_NAME:-inky-slideshow}"
SERVICE_USER="${INKY_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"

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
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

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
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  python3 -m venv --help >/dev/null 2>&1 || missing+=(python3-venv)

  if [[ "${#missing[@]}" -eq 0 ]]; then
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y git python3 python3-venv python3-pip
    return
  fi

  echo "Missing required commands: ${missing[*]}" >&2
  echo "Install git, python3, python3-venv, and python3-pip, then rerun this installer." >&2
  exit 1
}

install_os_packages

run_as_service_user mkdir -p "${INSTALL_DIR}" "${PHOTO_DIR}" "$(dirname "${CONFIG_PATH}")"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  run_as_service_user git -C "${INSTALL_DIR}" fetch --tags origin
  run_as_service_user git -C "${INSTALL_DIR}" checkout "${REPO_REF}"
  run_as_service_user git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_REF}"
else
  rmdir "${INSTALL_DIR}" 2>/dev/null || true
  run_as_service_user git clone --branch "${REPO_REF}" "${REPO_URL}" "${INSTALL_DIR}"
fi

run_as_service_user python3 -m venv "${INSTALL_DIR}/.venv"
run_as_service_user "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
run_as_service_user "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade "${INSTALL_DIR}"

TMP_UNIT="$(mktemp)"
cat >"${TMP_UNIT}" <<UNIT
[Unit]
Description=Inky Slideshow Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/src/inky_slideshow/slideshow.py "${PHOTO_DIR}" --config "${CONFIG_PATH}" --photo-seconds ${PHOTO_SECONDS} --weather-seconds ${WEATHER_SECONDS} --host ${WEB_HOST} --port ${WEB_PORT} --location-name "${LOCATION_NAME}" --latitude ${LATITUDE} --longitude ${LONGITUDE}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

"${SUDO[@]}" install -m 0644 "${TMP_UNIT}" "${UNIT_PATH}"
rm -f "${TMP_UNIT}"

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable "${SERVICE_NAME}.service"
"${SUDO[@]}" systemctl restart "${SERVICE_NAME}.service"

cat <<EOF
Installed ${SERVICE_NAME}.

Service:     ${SERVICE_NAME}.service
Install dir: ${INSTALL_DIR}
Photo dir:   ${PHOTO_DIR}
Config:      ${CONFIG_PATH}
Admin UI:    http://<frame-host>:${WEB_PORT}

Useful commands:
  sudo systemctl status ${SERVICE_NAME}.service
  sudo journalctl -u ${SERVICE_NAME}.service -f
EOF

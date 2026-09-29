#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo "WW14 Dashboard wird in $PROJECT_DIR eingerichtet."
chmod +x "$PROJECT_DIR/scripts/start-kiosk.sh"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 fehlt. Es wird über apt installiert."
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv
fi

if ! python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
  echo "Das Python-venv-Paket wird installiert."
  sudo apt-get update
  sudo apt-get install -y python3-venv
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "Eine neue .env-Datei wurde angelegt."
fi

mkdir -p "$HOME/.config/systemd/user"
sed "s|@@PROJECT_DIR@@|$PROJECT_DIR|g" \
  "$PROJECT_DIR/deploy/ww14-dashboard.service.template" \
  > "$HOME/.config/systemd/user/ww14-dashboard.service"

systemctl --user daemon-reload
systemctl --user enable --now ww14-dashboard.service

if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/autostart"
  sed "s|@@PROJECT_DIR@@|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/ww14-dashboard.desktop.template" \
    > "$HOME/.config/autostart/ww14-dashboard.desktop"
  echo "Der Vollbild-Browser startet bei der nächsten grafischen Anmeldung automatisch."
else
  echo "Hinweis: Chromium wurde nicht gefunden. Installiere es mit: sudo apt install chromium"
fi

echo
echo "Fertig. Das Dashboard läuft unter http://$(hostname -I | awk '{print $1}'):8080"
echo "Konfiguration: $PROJECT_DIR/config/dashboard.yaml"
echo "Zugangsdaten:   $PROJECT_DIR/.env"


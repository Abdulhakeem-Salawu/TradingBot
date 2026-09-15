#!/usr/bin/env bash
# MetaTrader 5 + its Python package on a headless Debian 12 VM, under Wine.
#
#   bash deploy/linux/install_mt5_wine.sh
#
# Follows MetaQuotes' own mt5linux.sh (WineHQ staging, WebView2, Windows 11
# mode, mt5setup.exe /auto) on a virtual screen, then adds Windows Python
# 3.11 from python.org with MetaTrader5 and rpyc from PyPI, and installs four
# systemd services: mt5-xvfb, mt5-wineserver, mt5-terminal, mt5-bridge.
# Takes 20-30 minutes on an e2-micro and about 4 GB of disk. Safe to re-run:
# finished steps are skipped.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$HOME/mt5"
export WINEPREFIX="$HOME/.mt5" WINEDEBUG=-all
URL_MT5="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
URL_WEBVIEW="https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/f2910a1e-e5a6-4f17-b52d-7faf525d17f8/MicrosoftEdgeWebview2Setup.exe"
PY_EXE="python-3.11.9-amd64.exe"
PY_MD5="e8dcd502e34932eebcaf1be056d5cbcd"      # from python.org's release page
TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
WIN_PY="$WINEPREFIX/drive_c/Python311/python.exe"

step() { echo; echo "== $(date -u +%H:%M:%S) $*"; }
mkdir -p "$WORK" && cd "$WORK" || exit 1

if ! command -v wine >/dev/null; then
  step "WineHQ staging + virtual display"
  sudo -E apt-get update -y -q
  sudo -E apt-get install -y -q wget curl gnupg xvfb xauth cabextract || exit 1
  sudo dpkg --add-architecture i386
  sudo mkdir -pm755 /etc/apt/keyrings
  wget -qO - https://dl.winehq.org/wine-builds/winehq.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/winehq-archive.key
  sudo wget -qNP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources
  sudo -E apt-get update -y -q
  sudo -E apt-get install -y -q --install-recommends winehq-staging || exit 1
fi
wine --version

if [ ! -f "$TERMINAL" ]; then
  export WINEDLLOVERRIDES="mscoree,mshtml="
  step "Wine prefix in Windows 11 mode"
  timeout 600 xvfb-run -a winecfg -v win11
  step "WebView2 runtime"
  curl -sSL -o webview2.exe "$URL_WEBVIEW" && timeout 900 xvfb-run -a wine webview2.exe /silent /install
  step "MetaTrader 5 (/auto)"
  # The installer launches the terminal when it finishes and exits non-zero
  # once xvfb-run tears the screen down; the file check below is what counts.
  curl -sSL -o mt5setup.exe "$URL_MT5" && timeout 1200 xvfb-run -a wine mt5setup.exe /auto
  wineserver -k 2>/dev/null
  unset WINEDLLOVERRIDES
fi
[ -f "$TERMINAL" ] || { echo "MetaTrader 5 did not install: $TERMINAL missing"; exit 1; }

if [ ! -f "$WIN_PY" ]; then
  step "Windows Python 3.11 in Wine"
  [ -f "$PY_EXE" ] || curl -sSLO "https://www.python.org/ftp/python/3.11.9/$PY_EXE"
  echo "$PY_MD5  $PY_EXE" | md5sum -c - || exit 1
  timeout 1200 xvfb-run -a wine "$PY_EXE" /quiet InstallAllUsers=0 'TargetDir=C:\Python311' PrependPath=0 \
    Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0
fi
step "MetaTrader5 + rpyc in Windows Python (rpyc must match the bot's venv)"
RPYC="$(grep -oE '^rpyc[^ #]*' "$ROOT/requirements.txt")"
timeout 1800 wine "$WIN_PY" -m pip install -q --disable-pip-version-check --no-warn-script-location \
  "MetaTrader5==5.0.6180" "$RPYC" || exit 1
wine "$WIN_PY" -c "import MetaTrader5, rpyc; print('MetaTrader5', MetaTrader5.__version__, 'rpyc', rpyc.__version__)"
"$ROOT/.venv/bin/python" -c "import rpyc; print('bot venv rpyc', rpyc.__version__)"
wineserver -k 2>/dev/null

step "systemd services"
for unit in mt5-xvfb mt5-wineserver mt5-terminal mt5-bridge; do
  sed -e "s|@USER@|$(id -un)|g" -e "s|@HOME@|$HOME|g" -e "s|@ROOT@|$ROOT|g" \
    "$ROOT/deploy/linux/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now mt5-xvfb mt5-wineserver mt5-terminal mt5-bridge
sleep 20
systemctl --no-pager --lines=0 status mt5-xvfb mt5-wineserver mt5-terminal mt5-bridge | grep -E "^.* mt5-|Active:"
free -m | sed -n 1,3p
du -sh "$WINEPREFIX"; df -h / | tail -1

cat <<EOF

Done. The terminal is running but not logged in yet. Add to $ROOT/.env
(nano .env; the file is chmod 600):

  MT5_BACKEND=wine
  MT5_TERMINAL_PATH=C:\\Program Files\\MetaTrader 5\\terminal64.exe
  MT5_LOGIN=<account number>
  MT5_PASSWORD=<password>
  MT5_SERVER=<server, e.g. MetaQuotes-Demo or Exness-MT5Trial>
  FX_DATA_SOURCE=mt5
  MT5_HISTORY_YEARS=10

then check: .venv/bin/python -m live.mt5_executor --status
EOF

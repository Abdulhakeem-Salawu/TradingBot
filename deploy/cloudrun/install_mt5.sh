#!/usr/bin/env bash
# MetaTrader 5 + Windows Python in the Wine prefix; run by bake_mt5.sh in the
# Cloud Run job signal-bot-bake (not in Cloud Build: see Dockerfile.wine).
#
# Follows MetaQuotes' own Linux script (Windows 11 mode, WebView2,
# mt5setup.exe /auto), then starts the terminal once so its first-run work --
# recompiling the bundled MQL5 programs and applying a live update -- happens
# here and not at the start of every Cloud Run job. Windows Python comes from
# python.org (checksum checked); MetaTrader5 and rpyc from PyPI.
#
# One virtual screen for all steps: tearing a screen down between steps kills
# Wine's background processes half way through setting up the prefix, and the
# next installer then hangs. A step that hangs prints its open windows every
# minute and, at its time limit, a screenshot as base64 PNG ("SCREEN" lines).
set -euo pipefail

URL_MT5="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
URL_WEBVIEW="https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/f2910a1e-e5a6-4f17-b52d-7faf525d17f8/MicrosoftEdgeWebview2Setup.exe"
PY_EXE="python-3.11.9-amd64.exe"
PY_MD5="e8dcd502e34932eebcaf1be056d5cbcd"          # python.org release page
MT5_PY="MetaTrader5==5.0.6180"
RPYC="rpyc==6.0.2"                                  # must match requirements.txt
MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
TERMINAL="$MT5_DIR/terminal64.exe"

step() { echo; echo "== $(date -u +%H:%M:%S) $*"; }
windows() { xwininfo -root -tree 2>/dev/null | grep -o '"[^"]*"' | sort -u | tr '\n' ' ' | cut -c1-400; }
screen() {
  echo "-- screenshot of the virtual screen: SCREEN $1 lines, base64 PNG"
  xwd -root -silent | xwdtopnm 2>/dev/null | pnmtopng 2>/dev/null | base64 -w 120 | sed "s/^/SCREEN $1 /" || true
}
never() { return 1; }
terminal_log_done() {
  for f in "$MT5_DIR/logs/"*.log; do
    [ -f "$f" ] && iconv -f UTF-16LE -t UTF-8 "$f" 2>/dev/null | grep -q "recompilation has been finished" && return 0
  done
  return 1
}
# supervise LABEL MAX_SECONDS DONE_FUNCTION COMMAND...: run COMMAND in the
# background until it exits or DONE_FUNCTION succeeds; at MAX_SECONDS, show the
# screen and give up (returns 1).
supervise() {
  local label=$1 max=$2 done_fn=$3 t=0 rc=0
  shift 3
  "$@" &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if "$done_fn"; then echo "-- $label: done after ${t}s"; return 0; fi
    if [ "$t" -ge "$max" ]; then
      echo "-- $label: still running after ${t}s; windows: $(windows)"
      screen "$label"
      kill "$pid" 2>/dev/null || true
      return 1
    fi
    if [ "$t" -gt 0 ] && [ $((t % 60)) -eq 0 ]; then echo "-- $label ${t}s; windows: $(windows)"; fi
    sleep 5
    t=$((t + 5))
  done
  wait "$pid" || rc=$?
  echo "-- $label: exit $rc after ${t}s"
}

cd /tmp
wine --version
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp >/dev/null 2>&1 &
XVFB=$!
trap 'wineserver -k 2>/dev/null || true; kill $XVFB 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.2; done

# No Wine Mono / Gecko: without these overrides Wine asks to download them in a
# dialog, which nobody can click on a virtual screen, and the installer hangs.
export WINEDLLOVERRIDES="mscoree,mshtml="

step "Wine prefix in Windows 11 mode"
timeout 600 winecfg -v win11
timeout 300 wineserver -w          # let the prefix set-up finish

step "WebView2 runtime"
curl -fsSL -o webview2.exe "$URL_WEBVIEW"
supervise webview2 900 never wine webview2.exe /silent /install || true

step "MetaTrader 5 (/auto)"
curl -fsSL -o mt5setup.exe "$URL_MT5"
supervise mt5setup 900 never wine mt5setup.exe /auto || true
wineserver -k || true              # the installer may have started the terminal
sleep 2
unset WINEDLLOVERRIDES
test -f "$TERMINAL" || { echo "MetaTrader 5 did not install"; ls -la "$WINEPREFIX/drive_c/Program Files/" || true; exit 1; }

step "first terminal start (compile bundled programs, live update)"
# A live update restarts the terminal, so wait for the log, not the process.
wine "C:\\Program Files\\MetaTrader 5\\terminal64.exe" >/dev/null 2>&1 &
t=0
until terminal_log_done; do
  if [ "$t" -ge 1800 ]; then echo "-- first run: no 'recompilation has been finished' after ${t}s"; screen first-run; break; fi
  if [ "$t" -gt 0 ] && [ $((t % 60)) -eq 0 ]; then echo "-- first run ${t}s; windows: $(windows)"; fi
  sleep 10
  t=$((t + 10))
done
sleep 90                           # let a live update that started meanwhile finish
wineserver -k || true
sleep 2
iconv -f UTF-16LE -t UTF-8 "$MT5_DIR/logs/"*.log | tail -15 || true

step "Windows Python 3.11 + $MT5_PY + $RPYC"
curl -fsSLO "https://www.python.org/ftp/python/3.11.9/$PY_EXE"
echo "$PY_MD5  $PY_EXE" | md5sum -c -
supervise python 1200 never wine "$PY_EXE" /quiet InstallAllUsers=0 'TargetDir=C:\Python311' PrependPath=0 \
  Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0 || exit 1
timeout 1800 wine 'C:\Python311\python.exe' -m pip install --no-cache-dir --disable-pip-version-check \
  --no-warn-script-location "$MT5_PY" "$RPYC"
wine 'C:\Python311\python.exe' -c "import MetaTrader5, rpyc; print('MetaTrader5', MetaTrader5.__version__, 'rpyc', rpyc.__version__)"

step "clean up"
wineserver -k || true
sleep 2
rm -rf /tmp/*.exe /root/.cache "$WINEPREFIX"/drive_c/users/*/AppData/Local/Temp/* "$MT5_DIR/logs/"*.log
du -sh "$WINEPREFIX"

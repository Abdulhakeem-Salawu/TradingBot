# Later option: MetaTrader 5 on Linux through Wine (24/7, no PC)

**Not the starting point.** The free e2-micro VM has 1 GB of RAM; MT5 under
Wine needs about 2 GB for one terminal. This runs MT5 and the executor on a
Linux VM that is always on, so your PC doesn't need to be. Expect roughly
$12/month for an e2-small (2 GB), or use Google's new-customer trial credit.
A Windows VPS running [WINDOWS_MT5.md](WINDOWS_MT5.md) costs about the same
and is officially supported. Choose this route if you prefer Linux.

## How it works

```
Linux VM (2 GB+)
  Xvfb (virtual screen) -> Wine -> MT5 terminal (logged in)
                                -> Windows Python + MetaTrader5 + pymt5linux server (127.0.0.1:8001)
  Linux Python: live.signal_job / live.compare (as on the free VM)
                live.mt5_executor --targets-source ledger   (MT5_BACKEND=mt5linux)
```

The executor reads targets straight from the local ledger, so no ssh is
involved. [`pymt5linux`](https://github.com/hpdeandrade/pymt5linux) mirrors
the official `MetaTrader5` API, so the adapter code is identical.

## Caveats

- **Community bridge.** MetaQuotes documents running the terminal on Wine, not
  Python inside Wine.
- **Updates can break it.** MT5 updates itself, and an update can break under
  Wine. Check `live.mt5_executor --status` after updates; the executor
  refuses to trade if it can't connect.
- **No screen for login dialogs.** Set `MT5_LOGIN`, `MT5_PASSWORD` and
  `MT5_SERVER` so the terminal logs in unattended. The password then lives
  in `.env` on the VM (chmod 600).
- **Security.** The RPyC server gives full control of the machine to anyone
  who can connect. Bind it to `127.0.0.1` only. The executor refuses any other
  `MT5_RPYC_HOST`, and never open port 8001 in the firewall.

## Setup outline (Debian 12, 2 GB+ VM)

```bash
sudo dpkg --add-architecture i386 && sudo apt-get update
sudo apt-get install -y wine64 wine32 xvfb
export DISPLAY=:99 && (Xvfb :99 -screen 0 1024x768x16 &)

# MT5 terminal: MetaQuotes' installer, or your broker's mt5setup.exe
wine mt5setup.exe /auto

# Windows Python inside Wine, with the bridge
wine python-3.11-amd64.exe /quiet InstallAllUsers=1 PrependPath=1
wine python -m pip install MetaTrader5 pymt5linux

# Linux side
cd ~/signal-monitoring && .venv/bin/pip install pymt5linux
```

Run these as systemd services so they restart on failure and boot:
1. `Xvfb :99`
2. `wine terminal64.exe /portable` (with `DISPLAY=:99`)
3. `wine python -m pymt5linux --host 127.0.0.1 --port 8001 <path-to-wine-python.exe>`

`.env` on the VM:

```
MT5_BACKEND=mt5linux
MT5_RPYC_HOST=127.0.0.1
MT5_RPYC_PORT=8001
MT5_LOGIN=12345678
MT5_PASSWORD=...
MT5_SERVER=YourBroker-Demo
TARGETS_SOURCE=ledger
ALLOW_LIVE_TRADING=false
```

Cron, next to the signal jobs:

```
*/10 * * * * cd ~/signal-monitoring && flock -n state/mt5.lock .venv/bin/python -m live.mt5_executor --targets-source ledger >> logs/mt5_executor.log 2>&1
```

Test in this order before enabling the cron line:
1. `python -m live.selftest`
2. `python -m live.mt5_executor --status --targets-source ledger`
3. `--check-costs`
4. `--dry-run`

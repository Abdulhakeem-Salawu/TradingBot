"""MetaTrader 5 on Linux: call the MetaTrader5 package running inside Wine.

The official `MetaTrader5` package only exists for Windows, so on a Linux VM
the terminal and a Windows Python run under Wine, and this module connects
the two halves over RPyC on 127.0.0.1:

  server  (Windows Python in Wine)   python live/mt5_bridge.py --port 8001
          imports MetaTrader5 and answers exactly two requests: call one of
          its functions, or read one of its constants. Results go back as
          plain data (numbers, strings, dicts, lists, raw array bytes), so the
          Linux side needs neither Windows nor the MetaTrader5 package.
  client  (the bot's Linux Python)   MT5_BACKEND=wine
          WineMT5 looks like the MetaTrader5 module: mt5.symbol_info(...)
          returns an object with the same attributes, copy_rates_range(...)
          a numpy structured array, mt5.ORDER_TYPE_BUY the constant.

Only official components run in Wine: Python from python.org, MetaTrader5 and
rpyc from PyPI, and this file. The server listens on 127.0.0.1 only; anything
that can reach the port can trade the logged-in account, so never forward or
expose it.

This file must stay importable by a bare Windows Python with only
MetaTrader5 and rpyc installed: no imports from the rest of the bot.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from types import SimpleNamespace

DEFAULT_PORT = 8001
LOCALHOST = ("127.0.0.1", "localhost", "::1")


# ------------------------------------------------------------------ plain data
def to_plain(obj):
    """MetaTrader5 results -> picklable data that needs no MetaTrader5 package to load."""
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj
    if hasattr(obj, "_asdict"):                      # SymbolInfo, Tick, TradePosition ...
        return {"__struct__": type(obj).__name__,
                "fields": {k: to_plain(v) for k, v in obj._asdict().items()}}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if type(obj).__module__ == "numpy":
        import numpy as np
        if isinstance(obj, np.ndarray):
            return {"__ndarray__": obj.dtype.descr, "shape": obj.shape, "data": obj.tobytes()}
        return obj.item()                             # numpy scalar
    return obj                                        # datetime and other stdlib types pickle fine


def from_plain(obj):
    if isinstance(obj, dict):
        if "__struct__" in obj:
            return SimpleNamespace(**{k: from_plain(v) for k, v in obj["fields"].items()})
        if "__ndarray__" in obj:
            import numpy as np
            dtype = np.dtype([tuple(f) for f in obj["__ndarray__"]])
            return np.frombuffer(obj["data"], dtype=dtype).reshape(obj["shape"]).copy()
        return {k: from_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return tuple(from_plain(v) for v in obj)
    return obj


# ---------------------------------------------------------------------- server
def serve(port: int = DEFAULT_PORT) -> None:
    import MetaTrader5 as mt5
    import rpyc
    from rpyc.utils.server import ThreadedServer

    class MT5Service(rpyc.Service):
        def exposed_call(self, name: str, payload: bytes) -> bytes:
            if name.startswith("_") or not callable(getattr(mt5, name, None)):
                raise AttributeError(f"MetaTrader5 has no function {name!r}")
            args, kwargs = pickle.loads(payload)
            fn = getattr(mt5, name)
            # order_send rejects a call carrying a keyword dict at all, even an
            # empty one, so only pass keywords when there are some.
            return pickle.dumps(to_plain(fn(*args, **kwargs) if kwargs else fn(*args)))

        def exposed_const(self, name: str) -> bytes:
            value = getattr(mt5, name)                # AttributeError travels back as-is
            if callable(value) or name.startswith("_"):
                raise AttributeError(f"MetaTrader5 has no constant {name!r}")
            return pickle.dumps(to_plain(value))

    print(f"MetaTrader5 {getattr(mt5, '__version__', '?')} bridge on 127.0.0.1:{port}", flush=True)
    ThreadedServer(MT5Service, hostname="127.0.0.1", port=port, reuse_addr=True,
                   protocol_config={"sync_request_timeout": 300}).start()


# ---------------------------------------------------------------------- client
class WineMT5:
    """Stands in for the MetaTrader5 module, forwarding every call to the bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 300):
        import rpyc

        if host not in LOCALHOST:
            raise ValueError(f"refusing MT5 bridge host {host!r}: it must be this machine")
        self._conn = rpyc.connect(host, port, config={"sync_request_timeout": timeout})
        self._consts: dict = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():                            # ORDER_TYPE_BUY, TIMEFRAME_H1 ...
            if name not in self._consts:
                self._consts[name] = from_plain(pickle.loads(self._conn.root.const(name)))
            return self._consts[name]

        def call(*args, **kwargs):
            raw = self._conn.root.call(name, pickle.dumps((args, kwargs)))
            return from_plain(pickle.loads(raw))
        call.__name__ = name
        return call

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 -- best effort
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="MetaTrader5 bridge server (run with Windows Python in Wine)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve(ap.parse_args().port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

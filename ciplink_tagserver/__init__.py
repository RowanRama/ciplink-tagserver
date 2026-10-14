"""One server per site that owns the PLC connections and serves tag values over a WebSocket."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ciplink-tagserver")  # from pyproject.toml, so it can't drift
except PackageNotFoundError:  # running from a checkout that isn't installed
    __version__ = "0.0.0"

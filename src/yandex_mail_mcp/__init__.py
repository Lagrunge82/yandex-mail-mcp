"""Yandex Mail tools for MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("yandex-mail-mcp")
except PackageNotFoundError:
    # Package metadata is unavailable when running from a source checkout.
    __version__ = "0.0.0"

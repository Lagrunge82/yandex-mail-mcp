"""Yandex Mail tools for MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("yandex-mail-mcp")
except PackageNotFoundError:  # Running from a source tree without installing the package.
    __version__ = "0.0.0"

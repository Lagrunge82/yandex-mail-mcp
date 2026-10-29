import tomllib
from pathlib import Path

import yandex_mail_mcp


def test_version_comes_from_package_metadata():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert yandex_mail_mcp.__version__ == expected

"""Determine whether the project version increased in a push to main."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from packaging.version import Version


def increased_version(previous: str, current: str) -> str | None:
    old, new = Version(previous), Version(current)
    if new < old:
        raise ValueError(f"Project version cannot decrease: {previous} -> {current}")
    if new == old:
        return None
    # The same name is used as a Git tag and a Docker tag. Local PEP 440
    # versions containing '+' are valid Python versions but not Docker tags.
    if not re.fullmatch(r"[0-9][A-Za-z0-9_.-]{0,126}", current):
        raise ValueError(f"Project version is not compatible with Docker tags: {current}")
    return current


def main() -> None:
    previous_sha = os.environ["PREVIOUS_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", previous_sha) or previous_sha == "0" * 40:
        raise ValueError("Cannot determine the previous main commit")
    old_content = subprocess.run(
        ["git", "show", f"{previous_sha}:pyproject.toml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    old = tomllib.loads(old_content)["project"]["version"]
    new = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    version = increased_version(old, new)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"bumped={'true' if version else 'false'}\n")
        if version:
            output.write(f"version={version}\n")


if __name__ == "__main__":
    main()

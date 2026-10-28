import runpy
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/version_change.py"
increased_version = runpy.run_path(str(SCRIPT))["increased_version"]


def test_version_increase_and_unchanged():
    assert increased_version("0.1.0", "0.1.1") == "0.1.1"
    assert increased_version("1.0rc1", "1.0") == "1.0"
    assert increased_version("1.0", "1.0.0") is None


def test_version_decrease_and_incompatible_docker_tag_are_rejected():
    with pytest.raises(ValueError, match="cannot decrease"):
        increased_version("0.2.0", "0.1.9")
    with pytest.raises(ValueError, match="not compatible"):
        increased_version("1.0", "1.0+build")


def test_script_compares_previous_commit_and_writes_outputs(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "0.1.0"\n')
    subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    previous = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    project.write_text('[project]\nversion = "0.1.1"\n')
    output = tmp_path / "output"
    env = dict(os.environ, PREVIOUS_SHA=previous, GITHUB_OUTPUT=str(output))
    subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env, check=True)
    assert output.read_text() == "bumped=true\nversion=0.1.1\n"

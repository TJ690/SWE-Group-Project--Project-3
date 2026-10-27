from __future__ import annotations

import subprocess
import sys

import pytest

from skillsig.cli import COMMANDS, main


def test_help_lists_all_stages() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "skillsig", "--help"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0
    for stage in COMMANDS:
        assert stage in proc.stdout
    assert set(COMMANDS) == {
        "data",
        "registry",
        "features",
        "dedup",
        "label",
        "sample",
        "evaluate",
        "analyze",
        "rq2",
        "export",
        "all",
    }


@pytest.mark.parametrize("stage", ["rq2"])
def test_unimplemented_stage_exits_2(stage: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([stage]) == 2
    assert f"not implemented: {stage}" in capsys.readouterr().err


def test_unimplemented_module_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["features", "--only", "does_not_exist", "--dev-sample"]) == 2
    assert "not implemented: features --only does_not_exist" in capsys.readouterr().err


def test_unknown_stage_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 2

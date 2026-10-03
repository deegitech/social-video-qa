"""The generated parts of docs/ must match the code and profile data."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from social_video_qa.util import ratio_label

ROOT = Path(__file__).resolve().parent.parent


def test_generated_docs_are_up_to_date() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_docs.py"), "--check"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr + "\nRun: python scripts/gen_docs.py"


def test_ratio_labels() -> None:
    assert ratio_label(9 / 16) == "9:16"
    assert ratio_label(16 / 9) == "16:9"
    assert ratio_label(0.01) == "1:100"
    assert ratio_label(0.1) == "1:10"
    assert ratio_label(10) == "10:1"
    assert ratio_label(886 / 1920) == "6:13"
    assert ratio_label(None) == "unknown"


def test_readme_mentions_every_builtin_profile_and_command() -> None:
    from social_video_qa.profiles import builtin_ids

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for pid in builtin_ids():
        assert f"`{pid}`" in readme, pid
    for command in ("svqa check", "svqa fix", "svqa profiles", "svqa probe", "svqa doctor"):
        assert command in readme, command

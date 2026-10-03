"""social-video-qa: check and fix videos against platform upload specs before you upload.

The command-line tool is ``svqa`` (also installed as ``social-video-qa``). The
small Python API below is provisional and may change before 1.0::

    from social_video_qa import analyze_file, check_file, load_profile

    profile = load_profile("instagram-reels-api")
    report = check_file("clip.mp4", profile)
    print(report.ok, [r.id for r in report.failures()])
"""

from __future__ import annotations

__version__ = "0.1.0"

from .api import analyze_file, check_file, load_profile

__all__ = ["__version__", "analyze_file", "check_file", "load_profile"]

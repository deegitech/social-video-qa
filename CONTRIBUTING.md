# Contributing

Thanks for helping. Bug reports, platform spec updates, new profiles, new checks and documentation fixes are all welcome.

## Ground rules

- Be kind; see the [Code of Conduct](CODE_OF_CONDUCT.md).
- Report security problems privately, as described in [SECURITY.md](SECURITY.md), not in issues.
- Never attach media you do not have the right to share. A few seconds of synthetic video made with FFmpeg's `lavfi`
  sources (see `tests/conftest.py`) is almost always enough to reproduce a problem.
- Do not commit real account IDs, tokens, file paths from your machine, or personal data in examples, tests or
  issues. Use obvious placeholders such as `com.example.mygame`.

## Development setup

```sh
git clone https://github.com/deegitech/social-video-qa
cd social-video-qa
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                     # unit tests run without FFmpeg; integration tests need ffmpeg/ffprobe
ruff check . && ruff format --check .
mypy src
python scripts/gen_docs.py --check
```

Integration tests generate tiny videos on the fly and are skipped automatically when FFmpeg is missing. CI runs them on
Python 3.10 to 3.14 with the FFmpeg from Ubuntu, on macOS with Homebrew's FFmpeg, and once without FFmpeg to make sure
the skips work.

## Updating a platform spec

1. Change the profile in `src/social_video_qa/data/profiles/`. Keep a `basis` on every rule:
   `documented` (with the source link in `sources`), `observed` (with a date in the `note`), `convention` or `derived`.
2. Update the `checked` date of the source you re-read, and add a note for anything ambiguous.
3. Run `python scripts/gen_docs.py` to refresh `docs/profiles.md`.
4. Add a line to `CHANGELOG.md` under "Unreleased".

Please do not present observations as documented rules. If a platform behaves differently from its own docs, record
both: the documented value in `notes`, and the observed one as the rule with `basis: observed`.

## Adding a check

1. Implement it in `src/social_video_qa/checks.py` with `@_register(...)`: an ID, a title, a default severity, typed
   parameters, `doc`, `hint`, and `needs` if it requires a decoding analysis.
2. If `svqa fix` can repair it, add it to `FIX_STRATEGY` and make sure the planner handles it.
3. Add unit tests on a hand-built `MediaInfo` (`tests/test_checks.py`) and, if it measures something new, an integration
   test with a synthetic file.
4. Run `python scripts/gen_docs.py` to refresh `docs/checks.md`.

## Adding or changing an error message

If people can hit it, tell them what to do. Add the one-line fix to `src/social_video_qa/hints.py`, a case to
`tests/test_hints.py`, and a row to `docs/troubleshooting.md`. If `svqa doctor` can detect the cause up front, add an
item to `src/social_video_qa/doctor.py` with its fix, and a test in `tests/test_doctor.py` that uses the fake FFmpeg
there.

## Pull requests

- Keep each pull request focused, and explain the user-visible change.
- Make sure `pytest`, `ruff`, `mypy` and `scripts/gen_docs.py --check` pass.
- New behaviour needs tests. Tests must stay offline and must not need real media or accounts.
- By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).

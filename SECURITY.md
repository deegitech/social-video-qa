# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## Reporting a vulnerability

Please report security problems **privately** through GitHub:

1. Open the repository's **Security** tab.
2. Click **Report a vulnerability** (GitHub private vulnerability reporting).
3. Describe the problem, how to reproduce it (a small sample file helps), and the impact you expect.

Please do not open a public issue, pull request or discussion for a security problem. We aim to acknowledge reports
within five working days. We will keep you informed while we work on a fix and credit you in the advisory, unless you
prefer not to be named.

## Scope

In scope:

- Anything that lets a crafted media or profile file make `svqa` read, write or delete files it should not, open
  network connections, run other programs, or escape the protections described in the README's *Security model*
  (protocol and demuxer whitelists, no shell, no in-place writes, atomic publishing, the overwrite rules).
- Ways to make `svqa fix` publish a file that failed verification, or overwrite a file it did not write.
- Secrets or personal data leaking into reports, the manifest or logs.

Out of scope:

- Vulnerabilities in FFmpeg itself. Please report those to the FFmpeg project (https://ffmpeg.org/security.html). If you
  find a way to reach an FFmpeg bug through `svqa` that the whitelists should have blocked, that part is in scope.
- Platform specifications being out of date. Please open a normal issue with the "Platform spec changed" template.

## Hardening notes for operators

If you run `svqa` on untrusted uploads (for example in a web service), run it in a container or sandbox with no network
access and a read-only view of everything except its output folder, and keep FFmpeg updated. `--timeout` bounds the
analysis steps only; the encodes of `svqa fix` have no time limit, so wrap `svqa fix` in `timeout(1)` or give the
container a CPU and time limit.

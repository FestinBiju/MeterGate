"""Fail without echoing material when tracked content resembles a real credential."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    re.compile(rb"rzp_(?:test|live)_[A-Za-z0-9]{8,64}"),
    re.compile(rb"(?:RAZORPAY_KEY_SECRET|RAZORPAY_WEBHOOK_SECRET|ENTITLEMENT_TOKEN_SECRET|ORBITINTEL_SHARED_SECRET)[ \t]*=[ \t]*[A-Za-z0-9_-]{32,256}"),
    re.compile(rb"mcp_[A-Za-z0-9_-]{43}"),
)


def synthetic(value: bytes) -> bool:
    lowered = value.lower()
    candidate = value.split(b"=", 1)[-1].strip()
    if lowered.startswith((b"rzp_test_", b"rzp_live_")):
        candidate = value.rsplit(b"_", 1)[-1]
    elif lowered.startswith(b"mcp_"):
        candidate = value[4:]
    return (
        any(
            marker in lowered
            for marker in (
                b"replace",
                b"change-me",
                b"example",
                b"placeholder",
                b"test-secret",
                b"test_secret",
                b"super-secret",
            )
        )
        or len(set(candidate)) < 8
        or bool(
        re.search(rb"(?:rzp_(?:test|live)_)?[0-9]{8,64}$", value)
        )
    )


def findings(payload: bytes) -> int:
    return sum(1 for pattern in PATTERNS for match in pattern.finditer(payload) if not synthetic(match.group(0)))


def run(*args: str) -> bytes:
    return subprocess.run(args, cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def main() -> int:
    tracked = run("git", "ls-files", "-z").split(b"\0")
    count = 0
    for raw_path in tracked:
        if not raw_path:
            continue
        path = ROOT / raw_path.decode("utf-8", errors="surrogateescape")
        try:
            count += findings(path.read_bytes())
        except OSError:
            continue
    # Scan committed patches too, but never print matching lines or values.
    try:
        count += findings(run("git", "log", "--all", "--format=", "-p", "--no-ext-diff"))
    except subprocess.CalledProcessError:
        pass
    if count:
        print(f"Secret scan failed: {count} credential-like value(s) require review.", file=sys.stderr)
        return 1
    print("Secret scan passed: no credential-like tracked values detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

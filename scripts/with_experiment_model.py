"""Run a Python evaluation using the isolated local experiment model profile."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".env.scu-ab"
PROFILE_KEYS = {
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_BETA_BASE_URL",
    "OPENAI_COMPATIBLE_MODEL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_STRUCTURED_OUTPUT",
    "OPENAI_COMPATIBLE_TIMEOUT_SECONDS",
    "OPENAI_COMPATIBLE_READ_TIMEOUT_SECONDS",
    "OPENAI_COMPATIBLE_TOTAL_TIMEOUT_SECONDS",
    "LAWAGENT_AGENT_REASONING_EFFORT",
    "LAWAGENT_SEMANTIC_REASONING_EFFORT",
    "LAWAGENT_SEMANTIC_MODEL",
}


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        raise SystemExit("Usage: python scripts/with_experiment_model.py -- <python arguments>")
    if not PROFILE.is_file():
        raise SystemExit("Missing local experiment profile: .env.scu-ab")
    values = {}
    for line in PROFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or key not in PROFILE_KEYS or key in values or not value.strip():
            raise SystemExit("Invalid experiment profile")
        values[key] = value.strip()
    if set(values) != PROFILE_KEYS:
        raise SystemExit("Incomplete experiment profile")
    environment = os.environ.copy()
    environment.update(values)
    hostname = urlsplit(values["OPENAI_COMPATIBLE_BASE_URL"]).hostname
    if not hostname:
        raise SystemExit("Invalid experiment endpoint")
    bypass = {item.strip() for key in ("NO_PROXY", "no_proxy")
              for item in environment.get(key, "").split(",") if item.strip()}
    bypass.add(hostname)
    environment["NO_PROXY"] = environment["no_proxy"] = ",".join(sorted(bypass))
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

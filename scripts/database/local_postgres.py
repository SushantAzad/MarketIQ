"""Control the existing workspace-local PostgreSQL verification cluster on Windows."""

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    executable = root / ".cache/postgres/runtime/pgsql/bin/pg_ctl.exe"
    cluster = root / "data/postgres"
    if not executable.is_file() or not (cluster / "PG_VERSION").is_file():
        parser.error("Portable cluster is absent; use the documented Docker Compose setup")
    command = [str(executable), "-D", str(cluster)]
    if args.action == "start":
        command += [
            "-l",
            str(root / "data/postgres-server.log"),
            "-o",
            "-h 127.0.0.1 -p 55432",
            "-w",
            "-t",
            "60",
            "start",
        ]
    elif args.action == "stop":
        command += ["-m", "fast", "-w", "-t", "60", "stop"]
    else:
        command += ["status"]
    # No shell interpolation, visible window, system-service registration, or new cluster creation.
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=False
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

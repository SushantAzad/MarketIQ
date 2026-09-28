"""Provision/start the official Windows Qdrant binary on loopback, without Docker."""

import argparse
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".cache" / "qdrant"
VERSION = "v1.19.1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["provision", "start", "status"])
    args = parser.parse_args()
    if args.command == "provision":
        with urllib.request.urlopen(
            f"https://api.github.com/repos/qdrant/qdrant/releases/tags/{VERSION}", timeout=60
        ) as response:
            release = json.load(response)
        asset = next(
            a for a in release["assets"] if a["name"] == "qdrant-x86_64-pc-windows-msvc.zip"
        )
        RUNTIME.mkdir(parents=True, exist_ok=True)
        archive = RUNTIME / asset["name"]
        urllib.request.urlretrieve(asset["browser_download_url"], archive)
        actual = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        if asset.get("digest") != actual:
            raise ValueError("Official release checksum does not match")
        with zipfile.ZipFile(archive) as bundle:
            # Only extract the executable, never arbitrary archive paths.
            name = next(n for n in bundle.namelist() if Path(n).name == "qdrant.exe")
            (RUNTIME / "qdrant.exe").write_bytes(bundle.read(name))
        (RUNTIME / "release.json").write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "url": asset["browser_download_url"],
                    "digest": actual,
                },
                indent=2,
            )
        )
        print(json.dumps({"provisioned": VERSION, "checksum_verified": True}))
    elif args.command == "start":
        try:
            with urllib.request.urlopen("http://127.0.0.1:6333/", timeout=2) as response:
                running = json.load(response)
        except urllib.error.URLError:
            running = None
        if running is not None:
            if running.get("title") != "qdrant - vector search engine":
                raise ValueError("Port 6333 is occupied by another service")
            print(json.dumps({"already_running": True, "version": running.get("version")}))
            return
        config = RUNTIME / "local.yaml"
        RUNTIME.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "log_level: WARN\ntelemetry_disabled: true\n"
            f'storage:\n  storage_path: "{(ROOT / "data/qdrant").as_posix()}"\n'
            f'  snapshots_path: "{(ROOT / "data/qdrant-snapshots").as_posix()}"\n'
            "service:\n  host: 127.0.0.1\n  http_port: 6333\n  grpc_port: 6334\n",
            encoding="utf-8",
        )
        with (RUNTIME / "server.log").open("ab") as log:
            process = subprocess.Popen(
                [str(RUNTIME / "qdrant.exe"), "--config-path", str(config)],
                cwd=RUNTIME,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        print(json.dumps({"started_pid": process.pid, "health_verified": False}))
    else:
        with urllib.request.urlopen("http://127.0.0.1:6333/", timeout=5) as response:
            print(response.read().decode())


if __name__ == "__main__":
    main()

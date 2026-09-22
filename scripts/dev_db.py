"""Run an isolated Windows PostgreSQL instance without installing a system service."""

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "postgres"
PG_VERSION = "17.11-1"
PG_URL = f"https://get.enterprisedb.com/postgresql/postgresql-{PG_VERSION}-windows-x64-binaries.zip"
BIN = RUNTIME / "pgsql" / "bin"
DATA = RUNTIME / "data"
PORT = "55432"


def runtime_drive() -> str:
    # PostgreSQL's Windows bootstrap mixes codepages in non-ASCII installation paths.
    # SUBST supplies an ASCII alias; every file remains in the project's .runtime.
    marker = RUNTIME / "runtime-drive.txt"
    if marker.exists():
        drive = marker.read_text(encoding="ascii").strip()
        if len(drive) != 2 or drive[0] not in "RSTUVWXYZ" or drive[1] != ":":
            raise SystemExit("Invalid PostgreSQL runtime drive marker")
    else:
        used = ctypes.windll.kernel32.GetLogicalDrives()
        drive = next(
            (letter + ":" for letter in "RSTUVWXYZ" if not used & (1 << (ord(letter) - 65))),
            None,
        )
        if drive is None:
            raise SystemExit("No free temporary drive letter for PostgreSQL")
    alias = Path(drive + "\\")
    if alias.exists():
        if not alias.samefile(RUNTIME):
            raise SystemExit(f"{drive} is used by a different location; leaving it unchanged")
    else:
        subprocess.run(
            ["subst", drive, str(RUNTIME)],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
    marker.write_text(drive, encoding="ascii")
    return drive


def run_pg(command: str, *args: str, env: dict[str, str] | None = None) -> None:
    drive = runtime_drive()
    command_line = [str(BIN / f"{command}.exe"), *args]
    command_line = [value.replace(str(RUNTIME), drive) for value in command_line]
    # A server child can inherit pipe handles and prevent communicate() from ending.
    output_path = RUNTIME / f"{command}.log"
    with output_path.open("wb") as output:
        result = subprocess.run(
            command_line,
            env=env,
            cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    message = output_path.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        raise SystemExit(message)
    print(message, end="", flush=True)


def start() -> None:
    run_pg(
        "pg_ctl",
        "-D",
        str(DATA),
        "-l",
        str(RUNTIME / "server.log"),
        "-o",
        f"-h 127.0.0.1 -p {PORT}",
        "-w",
        "start",
    )


def bootstrap() -> None:
    env_file = ROOT / ".env"
    if env_file.exists() or DATA.exists():
        raise SystemExit("Existing .env or DB data found; bootstrap does not overwrite them.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    archive = RUNTIME / f"postgresql-{PG_VERSION}.zip"
    if not (BIN / "postgres.exe").exists():
        print(f"Downloading PostgreSQL {PG_VERSION} from the official EDB host", flush=True)
        with urllib.request.urlopen(PG_URL, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        with archive.open("rb") as archive_file:
            digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                target = (RUNTIME / entry.filename).resolve()
                if not target.is_relative_to(RUNTIME.resolve()):
                    raise SystemExit("Archive path is outside the PostgreSQL runtime directory")
            bundle.extractall(RUNTIME)
        (RUNTIME / "download.json").write_text(
            json.dumps(
                {
                    "url": PG_URL,
                    "sha256": digest,
                    "downloaded_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    password = secrets.token_urlsafe(32)
    password_file = RUNTIME / "init-password.txt"
    password_file.write_text(password + "\n", encoding="utf-8")
    try:
        run_pg(
            "initdb",
            "-D",
            str(DATA),
            "-U",
            "finreg",
            "--encoding=UTF8",
            "--locale=C",
            "--auth=scram-sha-256",
            f"--pwfile={password_file}",
        )
    finally:
        password_file.unlink(missing_ok=True)
    env_file.write_text(
        f"FINREG_DATABASE_URL=postgresql+psycopg://finreg:{password}@127.0.0.1:{PORT}/finreg\n"
        f"FINREG_POSTGRES_PASSWORD={password}\n"
        "FINREG_DATABASE_CONNECT_TIMEOUT_SECONDS=3\n",
        encoding="utf-8",
    )
    start()
    pg_env = {**os.environ, "PGPASSWORD": password}
    for name in ("finreg", "finreg_test"):
        run_pg("createdb", "-h", "127.0.0.1", "-p", PORT, "-U", "finreg", name, env=pg_env)
    print("Local databases finreg and finreg_test are ready; credentials are in ignored .env.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "start", "stop", "status"))
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This helper is for Windows. Use compose.yaml on other platforms.")
    if args.action == "bootstrap":
        bootstrap()
    elif args.action == "start":
        start()
    elif args.action == "stop":
        run_pg("pg_ctl", "-D", str(DATA), "-m", "fast", "-w", "stop")
        subprocess.run(
            ["subst", runtime_drive(), "/D"],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
    else:
        run_pg("pg_ctl", "-D", str(DATA), "status")


if __name__ == "__main__":
    main()

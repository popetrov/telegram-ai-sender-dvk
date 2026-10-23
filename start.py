"""Shared Windows/macOS entry point. The owner may still run python main.py."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def environment_python(root=ROOT):
    return Path(root) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def command_status(command, *, root=ROOT):
    result = subprocess.run(command, cwd=str(root), check=False)
    return result.returncode


def prepare_environment(root=ROOT):
    root = Path(root)
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    python = environment_python(root)
    directory = root / "venv"
    if not directory.exists():
        venv.EnvBuilder(with_pip=True).create(directory)
    if not python.is_file():
        raise RuntimeError("Existing venv is incomplete. Preserve it and repair the Python installation first.")
    subprocess.run([str(python), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"],
                   cwd=str(root), check=True)
    requirements = root / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = directory / ".requirements.sha256"
    installed = marker.read_text(encoding="ascii").strip() if marker.exists() else ""
    if installed != digest:
        # Never upgrade pip implicitly, disable TLS checks, or alter the input file.
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)],
                       cwd=str(root), check=True)
        subprocess.run([str(python), "-m", "pip", "check"], cwd=str(root), check=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(digest + "\n", encoding="ascii")
        os.replace(temporary, marker)
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-updates", action="store_true", help="Run main.py without the auto-updater")
    parser.add_argument("--check", action="store_true", help="Inspect files without installation or launch")
    args = parser.parse_args(argv)
    if args.check:
        missing = [name for name in ("main.py", "launcher.py", "requirements.txt") if not (ROOT / name).is_file()]
        print(f"Python: {sys.version_info.major}.{sys.version_info.minor}")
        print("Virtual environment: " + ("present" if environment_python().is_file() else "not created"))
        print("Runtime files: " + (", ".join(missing) if missing else "present"))
        return int(bool(missing) or sys.version_info < (3, 11))
    python = prepare_environment()
    target = "main.py"
    return command_status([str(python), str(ROOT / target)])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Startup failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

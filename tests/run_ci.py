"""Preserve unittest exit status and expose diagnostics in Actions summaries."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    output = result.stdout + result.stderr
    reports = ROOT / "test-results"
    reports.mkdir(exist_ok=True)
    (reports / "unittest.txt").write_text(output, encoding="utf-8")
    # Console encoding on cmd can be narrower than UTF-8. Diagnostic writing
    # must not hide the original unittest failure or turn success into failure.
    sys.stdout.write(output.encode(sys.stdout.encoding or "utf-8", errors="backslashreplace")
                     .decode(sys.stdout.encoding or "utf-8"))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"## Unittest result: exit {result.returncode}\n\n```text\n{output[-24000:]}\n```\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

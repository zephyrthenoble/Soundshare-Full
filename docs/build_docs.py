"""Helper script to run the Doxygen build from a single command."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOXYFILE = REPO_ROOT / "docs" / "doxygen" / "Doxyfile"


def main() -> int:
    """Run Doxygen with the repository's configuration file."""
    doxygen = shutil.which("doxygen")
    if doxygen is None:
        doxygen = "C:\\Program Files\\doxygen\\bin\\doxygen.exe"
        if not Path(doxygen).exists():
            print(
                "error: 'doxygen' is not on PATH. Install it and retry.",
                file=sys.stderr,
            )
            return 1

    if not DOXYFILE.exists():
        print(f"error: missing Doxyfile at {DOXYFILE}", file=sys.stderr)
        return 1

    result = subprocess.run([doxygen, str(DOXYFILE)], cwd=REPO_ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

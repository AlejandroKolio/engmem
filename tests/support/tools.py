from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run_tool(
    tool: Path, store: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The Gate 1 tools are checked through their real command line, never by import.

    `encoding=` rather than the locale's: the tools write UTF-8 whatever the console code
    page says, and their output carries document quotes verbatim, so a reader that decodes
    by locale mangles every non-ASCII byte a store happens to contain. `env` overlays the
    inherited environment, e.g. to simulate a legacy console code page.
    """
    return subprocess.run(
        [sys.executable, str(tool), "--store", str(store)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={**os.environ, **env} if env else None,
    )

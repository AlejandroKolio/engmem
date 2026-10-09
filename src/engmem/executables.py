"""Finding an executable the way doctor and the snapshot lookup both need: never in the working
directory. Its own module because `runtime` prints to stdout, which the MCP server's imports must
not (tests/test_stdout_is_protocol_only.py)."""

from __future__ import annotations

import os
import sys


def on_path(name: str) -> str | None:
    """`shutil.which` without the current directory: Windows' lookup tries it before PATH, and a
    relative or empty PATH entry names it too — a file there is the project's, not an install."""
    names = [name]
    if sys.platform == "win32":
        extensions = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        suffixed = [name + extension for extension in extensions]
        # a name already ending in one of them is tried as given first, as `shutil.which` does
        has_extension = os.path.splitext(name)[1].casefold() in {e.casefold() for e in extensions}
        names = [name, *suffixed] if has_extension else suffixed
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        for candidate_name in names:
            candidate = os.path.join(entry, candidate_name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None

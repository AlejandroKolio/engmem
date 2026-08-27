"""M3: no file may be opened without an explicit encoding, asserted via `EncodingWarning` since
macOS defaults to UTF-8."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent

EXERCISE = """
import warnings
warnings.simplefilter("error", EncodingWarning)

from pathlib import Path
import tempfile

from engmem.spine import load_store
from engmem.scoring import search
from engmem.telemetry import log_search

fixtures = Path({fixtures!r})
result = load_store(fixtures)
assert result.docs, "fixtures must parse"

outcome = search(result.docs, "platform")

with tempfile.TemporaryDirectory() as td:
    log_search(Path(td) / "t.jsonl", query="platform", n_docs=len(result.docs), outcome=outcome)

print("OK")
"""


def test_no_default_encoding_opens_in_runtime_code():
    fixtures = str(Path(__file__).parent / "fixtures" / "sessions")
    proc = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-c",
         EXERCISE.format(fixtures=fixtures)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert proc.returncode == 0, (
        f"default-encoding open detected:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout

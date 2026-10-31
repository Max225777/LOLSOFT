import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "web"))

from app import app  # noqa: F401, E402

# Render sets PORT env var; uvicorn reads it from CLI, not here.
# This file is only the import shim.

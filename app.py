import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "web"))

from app import app  # noqa: F401, E402

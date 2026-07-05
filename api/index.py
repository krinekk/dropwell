import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mangum import Mangum  # noqa: E402

from dropwell.app import app  # noqa: E402

handler = Mangum(app)

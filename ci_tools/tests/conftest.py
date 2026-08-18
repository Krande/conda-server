import pathlib
import sys

# Make the package importable without an install (keeps `pixi run test-ci` and a
# bare `pytest ci_tools/tests` working from the repo root).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

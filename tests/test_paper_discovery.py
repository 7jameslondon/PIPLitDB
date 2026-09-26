"""Include the feature-local discovery tests in the repository's unittest suite."""

from pathlib import Path


def load_tests(loader, tests, pattern):
    root = Path(__file__).resolve().parents[1]
    return loader.discover(str(root / "paper_discovery/tests"), pattern="test_*.py", top_level_dir=str(root))

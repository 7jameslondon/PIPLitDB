import sys

if sys.version_info < (3, 12):
    raise SystemExit("Paper discovery requires Python 3.12 or later. Use the project's Python interpreter.")

try:
    from .src.cli import main
except ModuleNotFoundError as exc:
    if exc.name == "yaml":
        raise SystemExit("PyYAML is missing. Install the project requirements with this Python interpreter.") from exc
    raise

raise SystemExit(main())

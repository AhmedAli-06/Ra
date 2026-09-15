"""Ra CLI entry point: `python ra_cli.py ...` from the ra/ folder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ra.rag import cli  # noqa: E402

if __name__ == "__main__":
    cli.main()
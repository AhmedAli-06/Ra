"""Ra entry point: `python run_app.py` from the ra/ folder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ra import assistant  # noqa: E402

if __name__ == "__main__":
    assistant.main()
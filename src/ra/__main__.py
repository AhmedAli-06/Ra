"""Allow `python -m ra` when src/ is on sys.path (or after pip install -e .)."""
from ra import assistant

if __name__ == "__main__":
    assistant.main()
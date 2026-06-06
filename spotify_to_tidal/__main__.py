"""Allow `python -m spotify_to_tidal ...` invocation."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())

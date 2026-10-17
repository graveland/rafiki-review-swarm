import sys
from pathlib import Path

from review_swarm.cli import review_main

sys.exit(review_main(["--home", str(Path(__file__).resolve().parents[1]), *sys.argv[1:]]))

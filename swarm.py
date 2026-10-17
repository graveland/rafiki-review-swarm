#!/usr/bin/env python3
"""Run the review-swarm CLI from a checkout: `python swarm.py ...` is `review-swarm ...`."""

import sys

from review_swarm.cli import swarm_main

if __name__ == "__main__":
    sys.exit(swarm_main())

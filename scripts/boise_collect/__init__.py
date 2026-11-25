"""Local Boise collector; intentionally independent of the production service."""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("The Boise collector requires Python 3.11 or newer")

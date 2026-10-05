"""prack: paraglider tracker (OGN live feed to SQL, minimal live map)."""

import sys

if sys.version_info < (3, 11):  # noqa: UP036 - before anything imports `datetime.UTC` and fails with a puzzling ImportError
    raise SystemExit(
        f"prack needs Python 3.11 or newer, this is Python {sys.version_info[0]}.{sys.version_info[1]}. "
        "See the Linux section of the README for how to get one (or use the Docker image)."
    )

__version__ = "0.1.0"

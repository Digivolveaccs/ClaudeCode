#!/usr/bin/env bash
# The suite is stdlib unittest and touches no network.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -t . "$@"

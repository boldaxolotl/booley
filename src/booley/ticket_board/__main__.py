"""Entry point for ``python -m ticket_board``."""

import sys

from .cli import main

sys.exit(main())

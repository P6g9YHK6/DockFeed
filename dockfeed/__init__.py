"""One Atom feed with the release notes of every container running on a Docker host."""

import os

__version__ = os.environ.get("DOCKFEED_VERSION", "dev")

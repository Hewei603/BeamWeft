"""Single source of truth for the application version.

The launcher-started backend can outlive many code updates (the process keeps
serving old code until someone closes its window), so the frontend pins the
version it was built against in ``frontend/src/expectedBackend.js`` and shows a
"please restart the launcher" banner whenever ``/health`` reports anything
else — including nothing at all, which is what pre-2.1 backends report.
``tests/test_version_handshake.py`` asserts the two files agree.
"""

APP_VERSION = "1.9.0"
APP_NAME = "BeamWeft"


def provider_display_name(provider):
    """The stable built-in solver provider ID predates the product name."""
    return APP_NAME if provider == "LaserClaw" else provider

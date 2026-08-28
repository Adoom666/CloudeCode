"""Import-safe default values for the scalar ``AuthConfig`` fields.

WHY THIS IS A SEPARATE MODULE AND NOT JUST LITERALS ON THE MODEL

``src/config.py`` instantiates ``Settings()`` at import time and calls
``sys.exit(1)`` when ``.env`` is absent. That is correct for the server, whose
whole job depends on a valid environment, but it makes the module unimportable
from any tool that legitimately has no ``.env`` - notably
``scripts/config_upgrade.py``, which is invoked from the menu bar with the repo
root as its cwd.

The upgrade merge has to know what values upstream ships. Before this module
existed it could only look at ``config.example.json``, so a field that lived in
the model but had never been added to the example looked to it exactly like a
field upstream had DELETED. It reported ``REMOVED UPSTREAM`` for
``terminal_commands`` and ``config_version``, both of which are live and fully
supported. Copying the numbers into a second file would have fixed today's
symptom and guaranteed tomorrow's, because the copy drifts.

So the values live here, with no imports and no side effects, and
``AuthConfig`` consumes them. There is exactly one place each number is
written down, and both the server and a tool with no environment can read it.
"""

from __future__ import annotations

#: Schema/migration marker. Absent from every config.json written before
#: feat/launch-wrappers; treated as 0. See src/core/config_migration.py.
CONFIG_VERSION: int = 0

#: Legacy JWT lifetime in minutes. Used only when the access TTL is unset.
JWT_EXPIRY_MINUTES: int = 30

#: Access token lifetime (4 hours). Short by design so a leaked token has a
#: tight blast radius.
ACCESS_TOKEN_TTL_SECONDS: int = 14400

#: Refresh token lifetime (7 days). Long-lived but stored server-side with
#: rotation and reuse detection.
REFRESH_TOKEN_TTL_SECONDS: int = 604800

#: Grace window in which a just-rotated refresh token still works, so two
#: near-simultaneous refreshes do not trip reuse detection.
REFRESH_GRACE_SECONDS: int = 10

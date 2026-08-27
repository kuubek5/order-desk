"""Single source of truth for the application version.

`installer/OrderDesk.iss`'s `MyAppVersion` and `.github/workflows/release.yml`'s
hardcoded installer filename must be bumped by hand alongside this value at
release time — see `tests/test_version_sync.py`, which parses the `.iss` file
and fails the suite if the two drift apart instead of failing silently in
production.
"""

VERSION = "0.3.24"

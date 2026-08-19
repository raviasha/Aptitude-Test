"""Android build shim.

The student-facing mobile app authenticates with Google and never calls the
desktop password endpoints. Keeping this tiny module out of desktop builds
avoids shipping a native bcrypt wheel which the APK does not use.
"""


def _unsupported(*_args, **_kwargs):
    raise RuntimeError("Password authentication is unavailable in the Android app.")


hashpw = _unsupported
checkpw = _unsupported
gensalt = _unsupported

import marshal
from pathlib import Path

import pytest


def test_scanner_can_inspect_its_own_frozen_code_without_disabling_key_detection():
    from ksat import archive_inspection as scanner
    code = compile(Path(scanner.__file__).read_text(encoding="utf-8"), "scanner", "exec")
    scanner._assert_payload_safe("ksat.archive_inspection", marshal.dumps(code))
    for label in (b"PRIVATE", b"RSA PRIVATE"):
        with pytest.raises(ValueError, match="private/live"):
            scanner._assert_payload_safe("innocent.txt", b"-----BEGIN " + label + b" KEY-----")
    with pytest.raises(ValueError, match="forbidden entry"):
        scanner._assert_payload_safe("state/client.sqlite3", b"data")

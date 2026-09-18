"""Run the production HTTPS exam flow with the Linux device protector."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_distributed_end_to_end as protocol_tests


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Linux root build tests")
class LinuxProtocolTests(protocol_tests.DistributedEndToEndTests):
    def setUp(self):
        from ksat.client.linux_identity import LinuxKeyProtector
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        key = Path(self.directory.name) / "key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        protection = patch("ksat.client.identity._default_protector", side_effect=lambda: LinuxKeyProtector(key))
        protection.start()
        self.addCleanup(protection.stop)

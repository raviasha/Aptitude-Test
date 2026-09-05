import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock


class CoordinatorProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_process_thread_and_failed_release_cannot_break_owner(self):
        owner = CoordinatorProcessLock(self.data_dir).acquire()
        failures = []

        def contend():
            try:
                CoordinatorProcessLock(self.data_dir).acquire()
            except CoordinatorLockHeld:
                failures.append(True)

        thread = threading.Thread(target=contend)
        thread.start(); thread.join(2)
        self.assertEqual([True], failures)
        failed = CoordinatorProcessLock(self.data_dir)
        with self.assertRaises(CoordinatorLockHeld):
            failed.acquire()
        failed.release()
        with self.assertRaises(CoordinatorLockHeld):
            CoordinatorProcessLock(self.data_dir).acquire()
        owner.release(); owner.release()
        replacement = CoordinatorProcessLock(self.data_dir).acquire()
        replacement.release()
        self.assertTrue((self.data_dir / ".coordinator.lock").is_file())

    def test_metadata_pid_is_diagnostic_and_killed_process_auto_releases(self):
        lock_path = self.data_dir / ".coordinator.lock"
        lock_path.write_text(json.dumps({"pid": os.getpid(), "token": "old"}), encoding="ascii")
        first = CoordinatorProcessLock(self.data_dir).acquire()
        first.release()

        source_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys; from pathlib import Path; "
            "from ksat.coordinator.process_lock import CoordinatorProcessLock; "
            "lock=CoordinatorProcessLock(Path(sys.argv[1])).acquire(); "
            "print('LOCKED', flush=True); sys.stdin.read()"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root)
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.data_dir)],
            cwd=source_root, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual("LOCKED", child.stdout.readline().strip())
            with self.assertRaises(CoordinatorLockHeld):
                CoordinatorProcessLock(self.data_dir).acquire()
            child.kill(); child.wait(timeout=5)
            reacquired = CoordinatorProcessLock(self.data_dir).acquire()
            reacquired.release()
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()

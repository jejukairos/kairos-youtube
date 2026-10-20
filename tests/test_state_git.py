import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from kairos_youtube.errors import PolicyError
from kairos_youtube.state import GitCheckpoint, State, exclusive_lock


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def command(self, *args, cwd=None):
        subprocess.run(
            ["git", *args], cwd=cwd or self.root, check=True, capture_output=True
        )

    def client(self, name):
        folder = self.root / name
        folder.mkdir()
        self.command("init", cwd=folder)
        self.command(
            "remote", "add", "origin", str(self.root / "remote.git"), cwd=folder
        )
        return folder

    def test_cross_machine_restore_and_stale_writer_rejection(self):
        self.command("init", "--bare", "remote.git")
        a, b = self.client("a"), self.client("b")
        original = Path.cwd()
        try:
            os.chdir(a)
            ca = GitCheckpoint(a / "state.json", min_push_interval=0)
            s = State(a / "state.json", checkpoint=ca)
            s.data["videos"]["fixture"] = {"status": "sent"}
            s.save()
            os.chdir(b)
            cb = GitCheckpoint(b / "state.json", min_push_interval=0)
            restored = State(b / "state.json", checkpoint=cb)
            self.assertEqual("sent", restored.data["videos"]["fixture"]["status"])
            os.chdir(a)
            s.data["pause"] = {"reason": "new_revision"}
            s.save()
            os.chdir(b)
            restored.data["pause"] = {"reason": "stale_revision"}
            with self.assertRaises(PolicyError):
                restored.save()
        finally:
            os.chdir(original)

    def test_nested_lock_blocks_concurrent_delivery(self):
        path = self.root / "run.lock"
        with exclusive_lock(path):
            with self.assertRaises(PolicyError):
                with exclusive_lock(path):
                    self.fail("concurrent writer acquired lock")
        self.assertFalse(path.exists())

    def test_sending_marker_checkpoint_precedes_remote_send(self):
        calls = []
        s = State(
            self.root / "state.json",
            checkpoint=lambda raw: calls.append(json.loads(raw)),
        )
        s.data["videos"]["fixture"] = {"status": "sending"}
        s.save()
        self.assertEqual("sending", calls[0]["videos"]["fixture"]["status"])


if __name__ == "__main__":
    unittest.main()

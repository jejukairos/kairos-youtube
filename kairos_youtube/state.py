"""Atomic state; optional remote checkpoint BEFORE each external side effect."""

import copy
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import PolicyError


def git(*args, data=None, check=True):
    proc = subprocess.run(["git", *args], input=data, capture_output=True)
    if check and proc.returncode:
        detail = proc.stderr.lower()
        category = "git_error"
        for needles, label in (
            ((b"429", b"rate limit", b"too many requests"), "rate_limited"),
            ((b"stale info", b"non-fast-forward", b"fetch first"), "concurrent_writer"),
            (
                (b"could not resolve", b"connection", b"timed out", b"502", b"503"),
                "network",
            ),
            (
                (
                    b"permission denied",
                    b"403",
                    b"authentication failed",
                    b"protected branch",
                ),
                "permission",
            ),
        ):
            if any(n in detail for n in needles):
                category = label
                break
        operation = (
            args[0]
            if args
            and args[0]
            in {
                "push",
                "fetch",
                "ls-remote",
                "show",
                "hash-object",
                "mktree",
                "commit-tree",
                "count-objects",
                "config",
            }
            else "git"
        )
        # Never log stderr: remote URLs can contain authentication credentials.
        raise PolicyError(f"state_checkpoint_failed_{operation}_{category}")
    return proc.stdout.decode().strip(), proc.returncode


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PolicyError("concurrent_run_or_stale_lock") from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


class GitCheckpoint:
    branch = "refs/heads/kairos-youtube-state"

    def __init__(self, path, max_storage_bytes=104857600, *, min_push_interval=11):
        self.path = Path(path)
        self.max_storage_bytes = max_storage_bytes
        self.head = ""
        self.min_push_interval, self.last_push = min_push_interval, None
        listing, _ = git("ls-remote", "--exit-code", "origin", self.branch, check=False)
        if listing:
            self.head = listing.split()[0]
            git("fetch", "--no-tags", "origin", self.branch)
            raw, _ = git("show", f"{self.head}:state.json")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(raw + "\n", encoding="utf-8")
        else:
            # Distinguish missing branch from an inaccessible remote.
            git("ls-remote", "origin", "HEAD")
        git("config", "user.name", "Kairos state bot")
        git("config", "user.email", "kairos-state@users.noreply.github.com")

    def __call__(self, raw):
        blob, _ = git("hash-object", "-w", "--stdin", data=raw)
        tree, _ = git("mktree", data=f"100644 blob {blob}\tstate.json\n".encode())
        args = ["commit-tree", tree, "-m", "Checkpoint Kairos delivery and quota state"]
        if self.head:
            args.extend(["-p", self.head])
        commit, _ = git(*args)
        sizes, _ = git("count-objects", "-v")
        stats = dict(line.split(": ") for line in sizes.splitlines())
        if (
            int(stats.get("size", 0)) + int(stats.get("size-pack", 0))
        ) * 1024 >= self.max_storage_bytes:
            raise PolicyError("git_storage_limit_reached")
        if self.last_push is not None:
            remaining = self.min_push_interval - (time.monotonic() - self.last_push)
            if remaining > 0:
                time.sleep(remaining)
        git(
            "push",
            "--porcelain",
            f"--force-with-lease={self.branch}:{self.head}",
            "origin",
            f"{commit}:{self.branch}",
        )
        self.head = commit
        self.last_push = time.monotonic()


class State:
    def __init__(self, path, *, checkpoint=None, max_bytes=8_388_608):
        self.path = Path(path)
        self.checkpoint = checkpoint
        self.max_bytes = max_bytes
        self.last_saved = None
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "version": 1,
                "channels": {},
                "videos": {},
                "reservations": [],
                "errors": [],
                "pause": None,
                "briefings": {},
                "runs": [],
            }
        )
        if self.data.get("version") != 1:
            raise PolicyError("state_version_invalid")

    def save(self):
        raw = (
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        if len(raw) > self.max_bytes:
            raise PolicyError("storage_limit_reached")
        if raw == self.last_saved:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        # If the checkpoint fails, no caller may proceed to API/send.
        if self.checkpoint:
            self.checkpoint(raw)
        os.replace(tmp, self.path)
        self.last_saved = raw

    def error(self, code, now, video_id=None, channel_id=None):
        self.data["errors"].append(
            {
                "code": code,
                "at": now.isoformat(),
                "video_id": video_id,
                "channel_id": channel_id,
            }
        )
        self.data["errors"] = self.data["errors"][-300:]
        self.save()

    def recover(self):
        for v in self.data["videos"].values():
            if v["status"] == "analyzing":
                v["status"] = "pending"
            elif v["status"] == "sending":
                v["status"] = "delivery_uncertain"
        for b in self.data["briefings"].values():
            if b["status"] == "sending":
                b["status"] = "delivery_uncertain"
        for namespace in ("presentations", "load_alerts"):
            for outbox in self.data.get(namespace, {}).values():
                if outbox["status"] == "sending":
                    outbox["status"] = "delivery_uncertain"
        self.save()

    def snapshot(self):
        return copy.deepcopy(self.data)

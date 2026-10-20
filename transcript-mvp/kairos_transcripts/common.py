import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


class Problem(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def save_json(path, value):
    save_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def save_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False, suffix='.tmp') as stream:
            name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def timestamp(seconds):
    value = int(seconds)
    return f'{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}'

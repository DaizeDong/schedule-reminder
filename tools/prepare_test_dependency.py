"""Validate a supplied code-only test dependency against the committed content lock."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat


def regular_bytes(path):
    path = Path(path).absolute()
    for node in [*reversed(path.parents), path]:
        info = node.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)&1024
                or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError("test dependency must be an unaliased regular file")
    if not path.is_file():
        raise ValueError("test dependency is not a regular file")
    return path.read_bytes()


def validate(raw):
    spec = json.loads(regular_bytes(Path(__file__).parent/"test_dependencies.json"))["notification_language.py"]
    if len(raw) != spec["bytes"] or hashlib.sha256(raw).hexdigest() != spec["sha256"]:
        raise ValueError("canonical notification-language dependency content does not match its lock")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.source:
        raw = regular_bytes(args.source)
    else:
        encoded = os.environ.get("SCHEDULE_LANGUAGE_RULE_B64")
        if not encoded:
            raise ValueError("canonical notification-language CI dependency is missing; configure SCHEDULE_LANGUAGE_RULE_B64")
        raw = base64.b64decode(encoded, validate=True)
    validate(raw)
    destination = Path(args.out).absolute()
    for node in [*reversed(destination.parents), destination]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)&1024
                or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError("test dependency output cannot be aliased")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    print("Canonical notification-language dependency verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

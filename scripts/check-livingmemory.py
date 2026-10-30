#!/usr/bin/env python3
"""Read-only preflight for the pinned LivingMemory installation and safety patch."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(plugin_dir, lock=None):
    lock = lock or json.loads((ROOT/"compatibility/livingmemory.json").read_text(encoding="utf-8"))
    for relative, expected in lock["files"].items():
        path = plugin_dir/relative
        if not path.is_file():
            raise ValueError("missing pinned LivingMemory file: " + relative)
        # Original Windows plugin archives can use CRLF. Normalize only newlines.
        actual = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if actual != expected:
            raise ValueError("LivingMemory source/patch differs from pin: " + relative)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        check(args.plugin_dir)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+"\n")
    print("Pinned LivingMemory source and persona-isolation patch verified.")

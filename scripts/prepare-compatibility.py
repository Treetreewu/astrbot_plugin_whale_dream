#!/usr/bin/env python3
"""Rebuild exact dream hooks from pinned Git objects, never mutable upstream HEAD."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "maibot": ("MaiBot", "2a9af62cec37035d29237acbe2cf3adceb700b27", [
        "src/A_memorix/host_service.py", "src/common/utils/utils_message.py", "src/maisaka/chat_loop_service.py",
        "src/A_memorix/core/storage/metadata_store.py"]),
    "livingmemory": ("livingmemory", "c486ed65c7d072344c698f83359098c1d4293c8d", [
        "core/managers/consolidation_manager.py"]),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source_root):
    manifest = {"pins": {}, "files": {}, "patches": {}, "native_plugin": {}}
    for name, (repo, commit, files) in PINS.items():
        build = ROOT/".local/dream-build"/name
        build.mkdir(parents=True, exist_ok=True)
        manifest["pins"][name] = commit
        for relative in files:
            target = build/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "-C", str(source_root/repo), "show", commit+":"+relative]))
        patch = ROOT/"compatibility/patches"/f"dream-{name}.patch"
        subprocess.run(["git", "init", "-q", str(build)], check=True)
        subprocess.run(["git", "-C", str(build), "apply", "--check", str(patch)], check=True)
        subprocess.run(["git", "-C", str(build), "apply", str(patch)], check=True)
        manifest["patches"][str(patch.relative_to(ROOT)).replace("\\", "/")] = sha(patch)
        for relative in files:
            target = build/relative
            compile(target.read_text(encoding="utf-8"), str(target), "exec")
            manifest["files"][str(target.relative_to(ROOT)).replace("\\", "/")] = sha(target)
        if name == "livingmemory":
            for relative in ["main.py", "core/managers/consolidation_manager.py", "core/managers/memory_engine.py", "core/managers/conversation_manager.py", "core/utils/__init__.py"]:
                original = subprocess.check_output(["git", "-C", str(source_root/repo), "show", commit+":"+relative])
                manifest["native_plugin"][relative] = hashlib.sha256(original.replace(b'\r\n', b'\n')).hexdigest()
    target = ROOT/".local/dream-build/manifest.json"
    for relative in ("main.py", "metadata.yaml", "__init__.py"):
        path = ROOT/relative
        manifest["files"][path.relative_to(ROOT).as_posix()] = sha(path)
    target.write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    print("Pinned dream build inputs ready; native credentials/data untouched.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=Path, default=ROOT/".upstream")
    prepare(p.parse_args().source_root)

"""Local-only administration. No provider calls or QQ transport in this CLI."""
import argparse
import copy
import json
from pathlib import Path
import time

from .core import Dream
from .policy import day
from .runtime import config
from .store import Store


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", required=True, choices=["maibot", "astrbot"])
    p.add_argument("--state", type=Path)
    p.add_argument("command", choices=["status", "dry-run", "versions", "freeze", "unfreeze", "disable", "enable", "rollback"])
    p.add_argument("--record")
    p.add_argument("--version", type=int)
    a = p.parse_args()
    path = a.state or (Path("/MaiMBot/data/whale-dream/state.sqlite3") if a.bot == "maibot" else
                      Path("/AstrBot/data/plugin_data/astrbot_plugin_livingmemory/whale-dream/state.sqlite3"))
    if not path.is_file():
        p.error("dream state not initialized; refusing to create a fake runtime")
    store = Store(path, a.bot, time.time())
    try:
        cfg = config()
        dream = Dream(store, cfg, None)
        s = store.read()
        if a.command == "status":
            print(json.dumps(dict(bot=a.bot, enabled=dream.enabled(s), frozen=s["frozen"],
                rounds_today=sum(day(r["time"]) == day(time.time()) for r in s["rounds"]),
                scopes=len(s["scopes"]), pending_exports=sum(e["state"] != "done" for e in s["exports"].values()),
                jobs={status:sum(j["state"] == status for j in s["jobs"].values()) for status in ("pending", "done", "invalid")},
                active_traits=sum(r["kind"] == "trait" and r["promoted"] and r["status"] == "active" for r in s["records"].values())), ensure_ascii=False))
        elif a.command == "dry-run":
            print(json.dumps({"eligible": [{"scope_hash": k[:12], "messages":len(dream.snapshot(k)["messages"])} for k in dream.due()]}, ensure_ascii=False))
        elif a.command == "versions":
            rows = s["versions"]+list(s["records"].values())
            print(json.dumps([r for r in rows if (a.record is None or r["id"] == a.record)], ensure_ascii=False, indent=2))
        else:
            with store.edit() as s:
                if a.command in {"freeze", "unfreeze"}:
                    s["frozen"] = a.command == "freeze"
                elif a.command in {"disable", "enable"}:
                    s["disabled"] = a.command == "disable"
                else:
                    current = s["records"].get(a.record)
                    old = next((r for r in reversed(s["versions"]) if r["id"] == a.record and r["version"] == a.version), None)
                    if not current or not old or old["kind"] != "trait":
                        raise ValueError("choose an existing trait version")
                    if old["status"] not in {"candidate", "active"} or any(e["source_id"] in s["invalid_sources"] for e in old["evidence"]):
                        raise ValueError("invalidated evidence cannot be restored")
                    s["versions"].append(copy.deepcopy(current))
                    s["records"][a.record] = {**copy.deepcopy(old), "version":current["version"]+1, "updated":time.time()}
                    # Freeze on rollback so automatic consolidation cannot undo it.
                    s["frozen"] = True
                s["epoch"] += 1
            print(json.dumps({"status":"ok", "command":a.command}))
    finally:
        store.close()


if __name__ == "__main__":
    main()

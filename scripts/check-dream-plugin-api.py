#!/usr/bin/env python3
"""Check the candidate against pinned upstream Python contracts without bot startup."""
import argparse
import ast
import inspect
from pathlib import Path
import subprocess
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ASTR = "f61a7f42696da6df25dc7a249ec9b8d54ed9da18"
LIVING = "c486ed65c7d072344c698f83359098c1d4293c8d"


def source(root, repo, commit, path):
    return ast.parse(subprocess.check_output(["git", "-C", str(root/repo), "show", commit+":"+path]).decode("utf-8"))


def require_methods(tree, class_name, names):
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    methods = {node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not set(names) <= methods:
        raise ValueError(f"{class_name}: missing {sorted(set(names)-methods)}")


def check(root):
    public = source(root, "AstrBot", ASTR, "astrbot/api/event/filter/__init__.py")
    aliases = {alias.asname or alias.name: alias.name for node in public.body
               if isinstance(node, ast.ImportFrom) for alias in node.names}
    expected = {
        "on_astrbot_loaded": ("ready", "OnAstrBotLoadedEvent", 0),
        "on_plugin_loaded": ("plugin_loaded", "OnPluginLoadedEvent", 0),
        "on_plugin_unloaded": ("plugin_unloaded", "OnPluginUnloadedEvent", 0),
        "on_llm_request": ("request", "OnLLMRequestEvent", -100),
        "after_message_sent": ("sent", "OnAfterMessageSentEvent", -100),
    }
    registrations = []
    def register(handler, event, **kwargs):
        registrations.append((handler.__name__, event, kwargs.get("priority", 0)))

    upstream = source(root, "AstrBot", ASTR, "astrbot/core/star/register/star_handler.py")
    functions = [node for node in upstream.body if isinstance(node, ast.FunctionDef)
                 and node.name in {aliases[name] for name in expected}]
    namespace = {"get_handler_or_create": register,
                 "EventType": SimpleNamespace(**{value[1]: value[1] for value in expected.values()})}
    # Execute the exact upstream decorator bodies, with only registry storage
    # substituted. This verifies API names, event types and priority forwarding.
    exec(compile(ast.Module(body=functions, type_ignores=[]), "pinned_astrbot_decorators", "exec"), namespace)
    entry = ast.parse((ROOT/"main.py").read_text(encoding="utf-8"))
    cls = next(node for node in entry.body if isinstance(node, ast.ClassDef))
    namespace.update(filter=SimpleNamespace(**{name: namespace[aliases[name]] for name in expected}),
                     Star=object, Context=object, AstrMessageEvent=object, ProviderRequest=object)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "dream_entry", "exec"), namespace)
    if sorted(registrations) != sorted(expected.values()):
        raise ValueError(f"unexpected event registrations: {registrations}")
    plugin = namespace[cls.name]
    for name in ("initialize", "terminate", *(row[0] for row in expected.values())):
        if not inspect.iscoroutinefunction(getattr(plugin, name)):
            raise ValueError("non-async callback: " + name)
    require_methods(source(root, "AstrBot", ASTR, "astrbot/core/star/context.py"), "Context", ["get_registered_star"])
    require_methods(source(root, "livingmemory", LIVING, "main.py"), "LivingMemoryPlugin", ["terminate"])
    require_methods(source(root, "livingmemory", LIVING, "core/managers/memory_engine.py"), "MemoryEngine",
                    ["add_memory", "update_memory", "delete_memory", "batch_delete_memories", "archive_memories"])
    require_methods(source(root, "livingmemory", LIVING, "core/managers/conversation_manager.py"), "ConversationManager",
                    ["add_message_from_event"])
    require_methods(source(root, "livingmemory", LIVING, "core/managers/consolidation_manager.py"), "MemoryConsolidationManager",
                    ["maybe_run", "run_consolidation"])
    print("Pinned AstrBot event registration and LivingMemory method contracts verified; no container started.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT/".upstream")
    check(parser.parse_args().source_root)

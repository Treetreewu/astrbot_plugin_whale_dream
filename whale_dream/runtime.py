"""Pinned host integrations share a reentrant native-write gate and private state."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import functools
import json
import logging
import os
from pathlib import Path
import time

from .core import Dream
from .policy import validate_config
from .store import Store

LOG = logging.getLogger("whale_dream")


class InstanceHooks:
    """Own only our instance overrides, and release them on reload/failure."""

    def __init__(self):
        self.items = []

    def replace(self, obj, name, replacement):
        if not callable(getattr(obj, name, None)):
            raise RuntimeError("pinned native method missing: " + name)
        owned = name in vars(obj)
        previous = vars(obj).get(name)
        self.items.append((obj, name, owned, previous, replacement))
        setattr(obj, name, replacement)

    def restore(self):
        for obj, name, owned, previous, replacement in reversed(self.items):
            # Another extension may have replaced us since installation.
            if vars(obj).get(name) is replacement:
                if owned:
                    setattr(obj, name, previous)
                else:
                    delattr(obj, name)
        self.items.clear()


def config():
    path = (Path(os.environ["WHALE_DREAM_CONFIG"]) if "WHALE_DREAM_CONFIG" in os.environ
            else Path(__file__).with_name("default_config.json"))
    return validate_config(json.loads(path.read_text(encoding="utf-8-sig")))


class NativeGate:
    def __init__(self, lock=None):
        # LivingMemory supplies its task-reentrant SQLite lock. Using the same
        # lock also covers native callers that enter through cleanup/maintenance.
        self.lock = lock if lock is not None else asyncio.Lock()
        self.hooks = InstanceHooks()
        self.active = True
        self.owner = ContextVar("dream_native_owner", default=None)
        self.internal = ContextVar("dream_internal_write", default=False)

    @asynccontextmanager
    async def enter(self, internal=False):
        task = asyncio.current_task()
        nested = self.owner.get() is task
        if not nested:
            await self.lock.acquire()
        token = self.owner.set(task)
        inner = self.internal.set(internal or (nested and self.internal.get()))
        try:
            yield
        finally:
            self.internal.reset(inner)
            self.owner.reset(token)
            if not nested:
                self.lock.release()

    def wrap(self, obj, name, dream, invalidates=False):
        original = getattr(obj, name, None)
        if not callable(original):
            raise RuntimeError("pinned native method missing: " + name)

        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            async with self.enter():
                invalidate = False
                ids = None
                if self.active and invalidates and not self.internal.get():
                    # Fail closed on general native mutations; the host still
                    # decides authorization. Read-only admin actions do not enter.
                    action = kwargs.get("action", "")
                    readonly = {"preview", "plan", "get", "list", "status", "get_operation", "list_operations", "recycle_bin", "stats"}
                    if action not in readonly:
                        invalidate = True
                        ids = kwargs.get("memory_ids")
                        single = kwargs.get("memory_id")
                        if ids is None and single is not None:
                            ids = [single]
                        if ids is None and args and isinstance(args[0], (int, list, tuple)):
                            ids = args[0] if isinstance(args[0], (list, tuple)) else [args[0]]
                        if name == "update_memory":
                            updates = kwargs.get("updates", args[1] if len(args) > 1 else {})
                            invalidate = bool(set(updates) & {"content", "metadata"})
                result = await original(*args, **kwargs)
                success = result.get("success", True) if isinstance(result, dict) else result is not False
                if invalidate and success:
                    dream.invalidate_native(ids)
                return result
        self.hooks.replace(obj, name, wrapped)


def create(bot, folder, adapter):
    cfg = config()
    # Optional private mapping for transports that do not identify bot authors.
    # This lives beside the journal, never in the public deployment config.
    identities = Path(folder)/"whale-dream"/"sender-roles.json"
    if identities.exists():
        cfg = {**cfg, "sender_roles": {**cfg.get("sender_roles", {}),
            bot: json.loads(identities.read_text(encoding="utf-8-sig"))}}
        validate_config(cfg)
    store = Store(Path(folder)/"whale-dream"/"state.sqlite3", bot, time.time())
    dream = Dream(store, cfg, adapter)
    adapter.dream = dream
    return dream


def ensure_export_current(dream, record):
    state = dream.store.read()
    job = state["jobs"].get(record["job"])
    if not job or job["state"] == "invalid" or (job["state"] == "pending" and (
            job["epoch"] != state["epoch"] or job["revision"] != state["scopes"][job["scope"]]["revision"])):
        raise RuntimeError("dream native export lost its source version")


def safe_observe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        LOG.error("dream status=capture_failed error_type=%s", type(exc).__name__)

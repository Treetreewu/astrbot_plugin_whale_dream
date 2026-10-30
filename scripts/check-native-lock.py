"""Exercise Dream against pinned LivingMemory's actual lock and cleanup method."""
import argparse
import ast
import asyncio
import logging
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from whale_dream.astrbot import AstrAdapter

PIN = "c486ed65c7d072344c698f83359098c1d4293c8d"


def source(root, path):
    return ast.parse(subprocess.check_output(["git", "-C", str(root/"livingmemory"), "show", PIN+":"+path]).decode())


async def check(root):
    # The lock helper only needs aiosqlite for connections; no connection is
    # opened here. Execute its real lock/decorator bodies without that import.
    tree = source(root, "storage/sqlite_utils.py")
    tree.body = [n for n in tree.body if not (isinstance(n, ast.Import) and any(a.name == "aiosqlite" for a in n.names))]
    storage = ModuleType("native_fixture.storage.sqlite_utils")
    exec(compile(tree, "pinned_sqlite_utils", "exec"), storage.__dict__)
    sys.modules[storage.__name__] = storage
    cls = next(n for n in source(root, "core/managers/memory_engine.py").body
               if isinstance(n, ast.ClassDef) and n.name == "MemoryEngine")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "cleanup_old_memories")
    namespace = dict(with_sqlite_lock=storage.with_sqlite_lock, asyncio=asyncio, time=time,
        logger=logging.getLogger("native-lock-check"), safe_float=lambda v, d: float(v),
        clamp_float=lambda v, default: float(v))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "pinned_cleanup", "exec"), namespace)
    entered, release = asyncio.Event(), asyncio.Event()

    class Cursor:
        async def fetchall(self):
            entered.set()
            await release.wait()
            return [{"id": 1, "metadata": {"create_time": 0, "importance": 0.1}}]

    class DB:
        async def execute(self, sql, args):
            return Cursor() if args[0] == 0 else NS(fetchall=AsyncMock(return_value=[]))

    class Engine:
        __module__ = "native_fixture.core.managers.memory_engine"
        db_path = "synthetic-lock-check.sqlite3"
        config = {}
        db_connection = DB()
        _normalize_batch_metadata = staticmethod(lambda rows: rows)
        cleanup_old_memories = namespace["cleanup_old_memories"]

        @storage.with_sqlite_lock(lambda self: self.db_path)
        async def add_memory(self, **kwargs):
            return 42

        @storage.with_sqlite_lock(lambda self: self.db_path)
        async def batch_delete_memories(self, memory_ids):
            return len(memory_ids)

    engine = Engine()
    adapter = AstrAdapter(NS(initializer=NS(memory_engine=engine)))
    invalidations = []
    dream = NS(invalidate_native=invalidations.append)
    adapter.gate.wrap(engine, "add_memory", dream)
    adapter.gate.wrap(engine, "batch_delete_memories", dream, invalidates=True)
    cleanup = asyncio.create_task(engine.cleanup_old_memories())
    await entered.wait()
    writer = asyncio.create_task(engine.add_memory(content="synthetic"))
    await asyncio.sleep(0)
    release.set()
    try:
        assert await asyncio.wait_for(asyncio.gather(cleanup, writer), 2) == [1, 42]
        assert invalidations == [[1]]
    finally:
        for task in (cleanup, writer):
            task.cancel()
        await asyncio.gather(cleanup, writer, return_exceptions=True)
    print("Pinned native cleanup + concurrent Dream write + invalidation: PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    asyncio.run(check(parser.parse_args().source_root))

"""Offline lifecycle tests: real journal, no QQ transport or model credentials."""
import asyncio
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from whale_dream import astrbot
from whale_dream.astrbot_plugin import DreamBinding
from whale_dream.runtime import InstanceHooks

ROOT = Path(__file__).resolve().parents[1]


class NativeEngine:
    def __init__(self):
        self.rows = []
        self.db_connection = NS(execute=self.query)

    async def query(self, sql):
        return NS(fetchmany=AsyncMock(side_effect=[list(self.rows), []]), close=AsyncMock())

    async def add_memory(self, **kwargs):
        return 1

    async def update_memory(self, memory_id, updates):
        return True

    async def delete_memory(self, memory_id):
        return True

    async def batch_delete_memories(self, memory_ids):
        return True

    async def archive_memories(self, memory_ids):
        return True

class Consolidator:
    async def maybe_run(self, *args, **kwargs):
        return "native periodic"

    async def run_consolidation(self, *args, **kwargs):
        return "native manual"


class Conversation:
    async def add_message_from_event(self, event, role, content):
        return "native message"


class LivingMemory:
    __module__ = "test_livingmemory.main"

    def __init__(self):
        self.context = NS()
        self._terminating = False
        self.initializer = NS(is_initialized=True, memory_engine=NativeEngine(),
                              conversation_manager=Conversation())
        self.consolidation_manager = Consolidator()
        self.event_handler = NS()
        self.native_closed = False

    async def terminate(self):
        # The native store must not close before Dream releases it.
        if getattr(self, "_whale_dream", None) is not None:
            raise AssertionError("dream survived native shutdown")
        self._terminating = True
        self.native_closed = True


class BindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.host = LivingMemory()
        self.meta = NS(name="astrbot_plugin_livingmemory", activated=True,
                       version="2.7.0-gd.12", root_dir_name="astrbot_plugin_livingmemory",
                       star_cls=self.host)
        self.context = NS(get_registered_star=lambda name: self.meta if self.meta and name == self.meta.name else None)
        self.binding = DreamBinding(self.context)
        self.addAsyncCleanup(self.binding.stop)
        api, star, utils = ModuleType("astrbot.api"), ModuleType("astrbot.api.star"), ModuleType("test_livingmemory.core.utils")
        self.settings = {}
        async def get_async(**kwargs):
            return self.settings.get(kwargs["key"], {})
        api.sp = NS(get_async=get_async)
        self.data_dir = Mock(return_value=Path(self.folder.name))
        star.StarTools = NS(get_data_dir=self.data_dir)
        utils.get_persona_id = AsyncMock(return_value="unchanged-persona-id")
        self.utils = utils
        self.enterContext(patch.dict(sys.modules, {"astrbot.api": api, "astrbot.api.star": star,
                                                   "test_livingmemory.core.utils": utils}))
        self.enterContext(patch.dict(os.environ, WHALE_DREAM_CONFIG=str(ROOT/"whale_dream/default_config.json")))
        self.event = NS(unified_msg_origin="qq:group:unchanged-session-id", message_str="我喜欢乌龙茶",
                        message_obj=NS(message_id="m1", sender=NS(is_bot=False)), plugins_name=None,
                        get_messages=lambda: [], get_sender_id=lambda: "u1", get_self_id=lambda: "bot",
                        get_group_id=lambda: "g1")

    async def bind(self):
        await self.binding.start()
        self.assertIs(self.binding.host, self.host)
        return self.host._whale_dream

    async def test_delayed_start_reload_and_journal_identity(self):
        self.host.initializer.is_initialized = False
        await self.binding.start()
        self.assertIsNone(self.binding.host)
        self.assertFalse((Path(self.folder.name)/"whale-dream").exists())
        self.host.initializer.is_initialized = True
        dream = await self.bind()
        await self.host.initializer.conversation_manager.add_message_from_event(self.event, "user", "原文")
        original_sources = dream.store.read()["sources"]
        self.assertEqual(len(original_sources), 1)
        old_task, watcher = dream.task, self.binding.watcher
        await self.binding.start()
        self.assertIs(self.host._whale_dream, dream)
        self.assertIs(self.binding.watcher, watcher)
        await self.host.terminate()
        self.assertTrue(old_task.done())
        self.assertTrue(self.host.native_closed)
        self.host = LivingMemory()
        self.meta.star_cls = self.host
        await self.binding.reconcile()
        self.assertEqual(self.host._whale_dream.store.read()["sources"], original_sources)
        self.data_dir.assert_called_with("astrbot_plugin_livingmemory")

    async def test_disabled_or_wrong_version_never_installs(self):
        for field, value in (("version", "2.8.1"), ("activated", False), ("root_dir_name", "other")):
            previous = getattr(self.meta, field)
            setattr(self.meta, field, value)
            self.assertIsNone(await self.binding.reconcile())
            self.assertNotIn("_whale_dream", vars(self.host))
            setattr(self.meta, field, previous)
        self.meta = None
        self.assertIsNone(await self.binding.reconcile())

    async def test_missing_native_contract_rolls_back_all_overrides(self):
        engine = self.host.initializer.memory_engine
        engine.archive_memories = None
        with self.assertLogs("whale_dream", level="ERROR"):
            self.assertIsNone(await self.binding.reconcile())
        self.assertEqual(set(vars(engine)), {"archive_memories", "db_connection", "rows"})
        self.assertEqual(vars(self.host.consolidation_manager), {})
        self.assertIsNone(self.host._whale_dream)
        self.assertIsNone(astrbot._runtime)
        del engine.archive_memories
        await self.bind()

    async def test_unload_restores_methods_and_retained_wrapper_is_safe(self):
        dream = await self.bind()
        engine = self.host.initializer.memory_engine
        retained = engine.delete_memory
        await engine.delete_memory(123)
        self.assertEqual(dream.store.read()["epoch"], 1)
        await self.binding.stop()
        self.assertEqual(set(vars(engine)), {"db_connection", "rows"})
        self.assertEqual(vars(self.host.consolidation_manager), {})
        self.assertEqual(vars(self.host.initializer.conversation_manager), {})
        self.assertNotIn("terminate", vars(self.host))
        self.assertEqual(await self.host.consolidation_manager.maybe_run(), "native periodic")
        self.assertTrue(await retained(123))
        with self.assertRaises(sqlite3.ProgrammingError):
            dream.store.read()

    async def test_unload_drains_native_mutation_before_closing_journal(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_delete(memory_id):
            entered.set()
            await release.wait()
            return True
        self.host.initializer.memory_engine.delete_memory = slow_delete
        dream = await self.bind()
        deleting = asyncio.create_task(self.host.initializer.memory_engine.delete_memory(123))
        await entered.wait()
        stopping = asyncio.create_task(self.binding.stop())
        await asyncio.sleep(0)
        terminating = asyncio.create_task(self.host.terminate())
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        self.assertFalse(self.host.native_closed)
        self.assertEqual(dream.store.read()["epoch"], 0)
        release.set()
        await asyncio.gather(deleting, stopping, terminating)
        self.assertTrue(self.host.native_closed)
        self.assertTrue(await self.host.initializer.memory_engine.delete_memory(456))

    async def test_manual_run_cancelled_before_native_terminate(self):
        dream = await self.bind()
        entered = asyncio.Event()
        async def slow_round():
            entered.set()
            await asyncio.Future()
        # Let the scheduler finish its initial empty round first.
        await asyncio.sleep(0)
        dream.run_once = slow_round
        manual = asyncio.create_task(self.host.consolidation_manager.run_consolidation(force=True))
        await entered.wait()
        await self.host.terminate()
        self.assertTrue(manual.cancelled())
        self.assertTrue(self.host.native_closed)

    async def test_capture_error_does_not_fail_native_message(self):
        await self.bind()
        self.utils.get_persona_id.side_effect = RuntimeError("private message must not appear in logs")
        with self.assertLogs("whale_dream", level="ERROR") as logs:
            result = await self.host.initializer.conversation_manager.add_message_from_event(self.event, "user", "private")
        self.assertEqual(result, "native message")
        self.assertNotIn("private", " ".join(logs.output))

    async def test_public_events_inject_and_confirm_only_after_send(self):
        dream = await self.bind()
        dream.recall = Mock(return_value="\nlearned growth")
        dream.delivered = Mock()
        req = NS(system_prompt="source persona")
        await self.binding.request(self.event, req)
        self.assertEqual(req.system_prompt, "source persona\nlearned growth")
        dream.delivered.assert_not_called()
        await self.binding.sent(self.event)
        dream.delivered.assert_called_once_with(self.event.unified_msg_origin, "unchanged-persona-id")

    async def test_session_switches_and_plugin_selection_block_all_paths(self):
        dream = await self.bind()
        dream.recall, dream.delivered = Mock(return_value="unsafe"), Mock()
        for name in ("astrbot_plugin_livingmemory", "astrbot_plugin_whale_dream"):
            self.settings["session_plugin_config"] = {self.event.unified_msg_origin: {"disabled_plugins": [name]}}
            await self.binding.request(self.event, NS(system_prompt="persona"))
            await self.binding.sent(self.event)
            self.assertFalse(await dream.adapter.allowed({"session": self.event.unified_msg_origin}))
        self.settings.clear()
        self.event.plugins_name = ["astrbot_plugin_whale_dream"]
        await self.binding.request(self.event, NS(system_prompt="persona"))
        await self.binding.sent(self.event)
        dream.recall.assert_not_called()
        dream.delivered.assert_not_called()
        self.assertFalse(dream.store.read()["sources"])

    async def test_suspended_event_does_not_access_closed_store(self):
        await self.bind()
        entered, release = asyncio.Event(), asyncio.Event()
        async def persona(*args):
            entered.set()
            await release.wait()
            return "unchanged-persona-id"
        self.utils.get_persona_id.side_effect = persona
        req = NS(system_prompt="persona")
        pending = asyncio.create_task(self.binding.request(self.event, req))
        await entered.wait()
        await self.binding.stop()
        release.set()
        await pending
        self.assertEqual(req.system_prompt, "persona")

    async def test_native_edit_while_unloaded_invalidates_on_reload(self):
        self.host.initializer.memory_engine.rows = [dict(id=42, text="old", metadata={})]
        dream = await self.bind()
        await self.binding.stop()
        self.host.initializer.memory_engine.rows[0]["text"] = "edited while bridge absent"
        self.binding = DreamBinding(self.context)
        self.addAsyncCleanup(self.binding.stop)
        dream = await self.bind()
        self.assertEqual(dream.store.read()["epoch"], 1)

    async def test_clean_reload_without_edits_preserves_evidence(self):
        dream = await self.bind()
        await self.binding.stop()
        self.binding = DreamBinding(self.context)
        self.addAsyncCleanup(self.binding.stop)
        dream = await self.bind()
        self.assertEqual(dream.store.read()["epoch"], 0)

    async def test_dirty_checkpoint_sets_conservative_forgetting_barrier(self):
        dream = await self.bind()
        await self.binding.stop()
        from whale_dream.store import Store
        with_store = Store(Path(self.folder.name)/"whale-dream/state.sqlite3", "astrbot", 0)
        with with_store.edit() as state:
            state["astrbot_native_checkpoint"]["attached"] = True
        with_store.close()
        self.binding = DreamBinding(self.context)
        self.addAsyncCleanup(self.binding.stop)
        dream = await self.bind()
        self.assertEqual(dream.store.read()["epoch"], 1)


class HookTests(unittest.TestCase):
    def test_restore_does_not_overwrite_another_extension(self):
        obj = NS(fn=lambda: "original")
        hooks = InstanceHooks()
        hooks.replace(obj, "fn", lambda: "dream")
        obj.fn = lambda: "later extension"
        hooks.restore()
        self.assertEqual(obj.fn(), "later extension")


if __name__ == "__main__":
    unittest.main()

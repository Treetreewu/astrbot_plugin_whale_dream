import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from whale_dream.core import Dream
from whale_dream.policy import day, digest, parse_changes, validate_config
from whale_dream.runtime import NativeGate
from whale_dream.store import Store

ROOT = Path(__file__).resolve().parents[1]


class Adapter:
    def __init__(self):
        self.calls = 0
        self.writes = {}
        self.archived = set()
        self.reply = {"changes": []}
        self.on_generate = None
        self.on_put = None
        self.fail = False

    async def allowed(self, p): return True
    async def persona(self, p): return "有主见的鲸鱼娘"
    async def memories(self, p, m, limit): return []
    async def unchanged(self, p, m): return True

    async def generate(self, system, prompt):
        self.calls += 1
        self.payload = json.loads(prompt)
        if self.on_generate:
            self.on_generate()
        if self.fail:
            raise TimeoutError("private error should not appear in logs")
        return json.dumps(self.reply, ensure_ascii=False)

    async def put(self, eid, record, p):
        self.writes.setdefault(eid, copy.deepcopy(record))
        if self.on_put:
            self.on_put()
        return eid

    async def remove(self, eid, nid):
        self.archived.add(eid)


class DreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1793318400.0
        self.cfg = validate_config(json.loads((ROOT/"whale_dream/default_config.json").read_text()))
        self.adapter = Adapter()
        self.path = Path(self.tmp.name)/"state.db"
        self.store = Store(self.path, "maibot", self.now)
        self.dream = Dream(self.store, self.cfg, self.adapter, lambda:self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def observe(self, sid="1", text="我现在喜欢乌龙茶", session="A", persona="P", sender="u1", group=True, human=True):
        self.dream.observe(session=session, persona=persona, group=group, source_id=sid,
            sender=sender, text=text, human=human)
        return digest(digest(session, persona), sid)

    def change(self, sid, kind="fact", key="tea", text="u1喜欢乌龙茶", **kw):
        source = self.store.read()["sources"][sid]
        return dict(kind=kind, key=key, text=text, subject="u1" if kind in {"fact", "impression"} else "",
            basis="self_statement", evidence=[dict(source_id=sid, quote=source["text"], stance="support")],
            context="认真求助时", exceptions="简单问题直接回答", topics=["乌龙茶", "求助"], **kw)

    async def run_changes(self, changes):
        self.adapter.reply = {"changes": changes}
        self.now += 1201
        return await self.dream.run_once()

    async def test_idle_quota_failed_round_and_restart(self):
        self.observe()
        self.assertEqual((await self.dream.run_once())["status"], "idle")
        self.adapter.fail = True
        await self.run_changes([])
        self.assertEqual(self.adapter.calls, 1)
        self.now += 21601
        await self.dream.run_once()
        self.assertEqual(self.adapter.calls, 2)
        self.store.close()
        self.store = Store(self.path, "maibot", self.now)
        self.dream = Dream(self.store, self.cfg, self.adapter, lambda:self.now)
        self.now += 21601
        await self.dream.run_once()
        self.assertEqual(self.adapter.calls, 2)

    async def test_no_input_and_unchanged_input_do_not_spend(self):
        await self.dream.run_once()
        self.assertEqual(self.adapter.calls, 0)
        self.observe()
        await self.run_changes([])
        self.now += 21601
        await self.dream.run_once()
        self.assertEqual(self.adapter.calls, 1)

    async def test_fact_native_write_and_scope_isolation(self):
        sid = self.observe(group=False)
        await self.run_changes([self.change(sid)])
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertIn("乌龙茶", self.dream.recall("A", "P", "乌龙茶", "u1"))
        for session, persona, sender in [("B", "P", "u1"), ("A", "Q", "u1"), ("A", "P", "u2")]:
            self.assertEqual(self.dream.recall(session, persona, "乌龙茶", sender), "")

    async def test_maibot_prompt_worker_injects_memory(self):
        from whale_dream import maibot
        sid = self.observe(persona=maibot.PERSONA)
        await self.run_changes([self.change(sid)])
        with patch.object(maibot, "_runtime", self.dream):
            text = await asyncio.to_thread(maibot._inject, "A")
        self.assertIn("乌龙茶", text)
        self.dream.delivered("A", maibot.PERSONA)
        next_source = self.observe("next", persona=maibot.PERSONA)
        self.assertIn(next_source, self.store.read()["sources"])

    def test_concurrent_journal_edits_preserve_all_updates(self):
        def increment():
            for _ in range(20):
                with self.store.edit() as state:
                    state["epoch"] += 1
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(increment) for _ in range(4)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(self.store.read()["epoch"], 80)

    def test_worker_read_waits_for_rollback(self):
        started = Event()
        def read():
            started.set()
            return self.store.read()
        with ThreadPoolExecutor(1) as pool:
            with self.assertRaisesRegex(ValueError, "abort"):
                with self.store.edit() as state:
                    state["epoch"] = 999
                    reader = pool.submit(read)
                    self.assertTrue(started.wait(timeout=2))
                    self.assertFalse(reader.done())
                    raise ValueError("abort")
            self.assertEqual(reader.result(timeout=2)["epoch"], 0)
        with self.store.edit() as state:
            state["epoch"] += 1
        self.assertEqual(self.store.read()["epoch"], 1)

    async def test_new_message_during_generation_discards(self):
        sid = self.observe()
        self.adapter.on_generate = lambda:self.observe("2", "又来了新信息")
        await self.run_changes([self.change(sid)])
        self.assertEqual(self.adapter.writes, {})
        self.assertEqual(self.store.read()["scopes"][digest("A", "P")]["cursor"], 0)

    async def test_new_message_during_native_write_is_archived(self):
        sid = self.observe()
        self.adapter.on_put = lambda:self.observe("2", "新信息")
        await self.run_changes([self.change(sid)])
        self.assertEqual(self.dream.recall("A", "P", "乌龙茶"), "")
        self.assertEqual(set(self.adapter.writes), self.adapter.archived)

    async def test_aborted_export_never_reuses_archived_id(self):
        sid = self.observe()
        self.adapter.on_put = lambda:self.observe("2", "新信息")
        await self.run_changes([self.change(sid)])
        old_ids = set(self.adapter.writes)
        self.adapter.on_put = None
        self.now += 21601
        await self.run_changes([self.change(sid)])
        current = next(iter(self.store.read()["records"].values()))
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["version"], 2)
        self.assertEqual(len(set(self.adapter.writes)-old_ids-self.adapter.archived), 1)

    async def test_known_native_edit_preserves_other_pending_scopes(self):
        sid = self.observe()
        await self.run_changes([self.change(sid)])
        self.now += 21601
        self.observe("b", session="B")
        self.now += 1201
        self.dream.invalidate_native(list(self.adapter.writes))
        self.assertEqual(self.dream.due(), [digest("B", "P")])
        self.dream.invalidate_native(["unrelated-id"])
        self.assertEqual(self.dream.due(), [digest("B", "P")])

    async def test_forget_during_generation_and_old_input_barrier(self):
        sid = self.observe()
        self.adapter.on_generate = lambda:self.dream.invalidate_native()
        await self.run_changes([self.change(sid)])
        self.assertFalse(self.adapter.writes)
        self.now += 21601
        self.assertFalse(self.dream.due())

    async def test_invalid_evidence_and_other_person_fact_rejected(self):
        sid = self.observe()
        msg = list(self.store.read()["sources"].values())
        change = self.change(sid)
        change["subject"] = "u2"
        with self.assertRaises(ValueError): parse_changes(json.dumps({"changes":[change]}), msg, [], self.cfg)
        change = self.change(sid)
        change["evidence"][0]["quote"] = "编造的原话"
        with self.assertRaises(ValueError): parse_changes(json.dumps({"changes":[change]}), msg, [], self.cfg)

    async def test_bot_and_duplicate_messages_not_evidence(self):
        sid = self.observe(human=False)
        self.observe(human=False)
        self.assertEqual(len(self.store.read()["sources"]), 1)
        with self.assertRaises(ValueError):
            parse_changes(json.dumps({"changes":[self.change(sid)]}), list(self.store.read()["sources"].values()), [], self.cfg)

    async def test_growth_needs_independent_days_and_feedback(self):
        for i in range(3):
            sid = self.observe(str(i), "认真求助先问清楚真的有用")
            await self.run_changes([self.change(sid, kind="trait", key="clarify", text="求助时先问清楚")])
            self.assertIn("求助", self.dream.recall("A", "P", "认真求助", "u1"))
            self.dream.delivered("A", "P")
            if i == 0:
                self.assertFalse(next(iter(self.store.read()["records"].values()))["promoted"])
            self.now += 86400
        record = next(iter(self.store.read()["records"].values()))
        self.assertTrue(record["promoted"])
        self.assertEqual(record["status"], "active")
        # Runtime exposure retention must not expire an accepted habit.
        with self.store.edit() as state:
            state["exposures"].extend([dict(record="unrelated", scope="other", time=self.now)]*2001)
        self.dream.prune()
        await self.dream.flush()
        self.assertTrue(next(iter(self.store.read()["records"].values()))["promoted"])
        self.store.close()
        self.store = Store(self.path, "maibot", self.now)
        self.assertTrue(next(iter(self.store.read()["records"].values()))["promoted"])

    async def test_many_messages_in_one_burst_not_three_interactions(self):
        ids = [self.observe(str(i), "求助先问清楚") for i in range(3)]
        c = self.change(ids[0], kind="trait")
        c["evidence"] = [dict(source_id=x, quote="求助先问清楚", stance="support") for x in ids]
        await self.run_changes([c])
        self.assertFalse(next(iter(self.store.read()["records"].values()))["promoted"])

    async def test_private_global_habit_never_crosses(self):
        sid = self.observe(group=False)
        await self.run_changes([self.change(sid, kind="trait", global_habit="clarify_first")])
        r = next(iter(self.store.read()["records"].values()))
        self.assertEqual(r["global_habit"], "")
        self.assertEqual(self.dream.recall("B", "P", "求助"), "")

    async def test_mood_strength_halves_without_becoming_trait(self):
        sid = self.observe()
        await self.run_changes([self.change(sid, kind="mood", text="对乌龙茶有兴致")])
        self.now += 21600
        self.assertIn("50%", self.dream.recall("A", "P", "乌龙茶"))
        self.now += 43201
        self.assertEqual(self.dream.recall("A", "P", "乌龙茶"), "")

    async def test_forget_cascades_and_retains_tombstones(self):
        sid = self.observe()
        await self.run_changes([self.change(sid)])
        self.dream.invalidate_native(list(self.adapter.writes))
        await self.dream.flush()
        self.assertEqual(set(self.adapter.writes), self.adapter.archived)
        self.assertIn(sid, self.store.read()["invalid_sources"])
        self.now += 9*86400
        self.dream.prune()
        self.assertIsNone(self.store.read()["sources"][sid]["text"])
        self.assertIn(sid, self.store.read()["invalid_sources"])

    async def test_crash_after_native_write_replay_is_idempotent(self):
        sid = self.observe()
        failed = False
        def fail_once():
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated journal ack gap")
        self.adapter.on_put = fail_once
        await self.run_changes([self.change(sid)])
        self.assertEqual(len(self.adapter.writes), 1)
        await self.dream.flush()
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertIn("乌龙茶", self.dream.recall("A", "P", "乌龙茶"))

    async def test_round_batches_five_scopes_and_bounded_input(self):
        for i in range(7): self.observe(str(i), "内容"*9000, session=str(i))
        await self.run_changes([])
        self.assertEqual(self.adapter.calls, 5)
        self.assertLessEqual(len(json.dumps(self.adapter.payload, ensure_ascii=False)), self.cfg["input_chars"])
        self.assertEqual(len(self.store.read()["rounds"]), 1)

    async def test_bot_only_prefix_cannot_starve_later_human_evidence(self):
        for i in range(125):
            self.observe(f"bot-{i}", "机器人上下文" * 2000, human=False)
        sid = self.observe("human", "我现在喜欢乌龙茶")
        result = await self.run_changes([self.change(sid)])
        self.assertEqual(result["results"], ["committed"])
        self.assertIn(sid, {m["id"] for m in self.adapter.payload["messages"]})
        self.assertIn("乌龙茶", self.dream.recall("A", "P", "乌龙茶", "u1"))
        self.assertFalse(self.dream.due())

    async def test_escaped_long_message_is_bounded_without_blocking_next_message(self):
        self.observe("large", '"' * 16000)
        sid = self.observe("next", "我现在喜欢乌龙茶")
        await self.run_changes([])
        self.assertEqual(self.adapter.calls, 1)
        self.assertLessEqual(len(json.dumps(self.adapter.payload, ensure_ascii=False)), self.cfg["input_chars"])
        self.now += self.cfg["round_gap_seconds"]
        result = await self.run_changes([self.change(sid)])
        self.assertEqual(result["results"], ["committed"])
        self.assertFalse(self.dream.due())

    def test_china_day_boundary(self):
        from datetime import datetime, timezone
        before = datetime(2026,10,30,15,59,59,tzinfo=timezone.utc).timestamp()
        self.assertEqual(day(before), "2026-10-30")
        self.assertEqual(day(before+1), "2026-10-31")

    async def test_self_correction_replaces_current_fact_and_archives_projection(self):
        sid = self.observe(text="我喜欢咖啡")
        await self.run_changes([self.change(sid, text="u1喜欢咖啡")])
        old = next(iter(self.store.read()["records"].values()))
        self.now += 21601
        sid = self.observe("2", "我现在改喝乌龙茶了，不喝咖啡")
        await self.run_changes([self.change(sid, correction=True, supersedes=[old["id"]])])
        current = next(iter(self.store.read()["records"].values()))
        self.assertEqual(current["text"], "u1喜欢乌龙茶")
        self.assertEqual(len(self.adapter.archived), 1)
        self.assertEqual(len(self.adapter.writes), 2)

    async def test_stale_correction_restores_previous_valid_fact(self):
        sid = self.observe(text="我喜欢咖啡")
        await self.run_changes([self.change(sid, text="u1喜欢咖啡")])
        old = next(iter(self.store.read()["records"].values()))
        self.now += 21601
        sid = self.observe("2", "我改喝乌龙茶了")
        self.adapter.on_put = lambda:self.observe("3", "还有一件事")
        await self.run_changes([self.change(sid, correction=True, supersedes=[old["id"]])])
        self.assertEqual(self.store.read()["records"][old["id"]]["text"], "u1喜欢咖啡")
        self.assertEqual(self.store.read()["records"][old["id"]]["status"], "active")

    async def test_unrelated_delete_aborts_pending_update_with_cleanup(self):
        sid = self.observe(text="我喜欢咖啡")
        await self.run_changes([self.change(sid, text="u1喜欢咖啡")])
        old = next(iter(self.store.read()["records"].values()))
        self.now += 21601
        sid = self.observe("2", "我改喝乌龙茶了")
        self.adapter.on_put = lambda:self.dream.invalidate_native(["unrelated-native-id"])
        await self.run_changes([self.change(sid, correction=True, supersedes=[old["id"]])])
        self.assertEqual(self.store.read()["records"][old["id"]]["text"], "u1喜欢咖啡")
        self.assertEqual(self.store.read()["records"][old["id"]]["status"], "active")
        self.assertEqual(len(self.adapter.archived), 1)

    async def test_contradiction_without_correction_does_not_overwrite(self):
        sid = self.observe(text="我喜欢咖啡")
        await self.run_changes([self.change(sid, text="u1喜欢咖啡")])
        self.now += 21601
        sid = self.observe("2", "我喜欢乌龙茶")
        await self.run_changes([self.change(sid)])
        rows = list(self.store.read()["records"].values())
        self.assertEqual({r["kind"] for r in rows}, {"fact", "conflict"})
        self.assertEqual(next(r for r in rows if r["kind"] == "fact")["text"], "u1喜欢咖啡")

    async def test_prompt_exposure_without_delivery_does_not_promote(self):
        for i in range(3):
            sid = self.observe(str(i), "求助先问清楚很好")
            await self.run_changes([self.change(sid, kind="trait")])
            self.dream.recall("A", "P", "认真求助")
            self.now += 86400
        self.assertFalse(next(iter(self.store.read()["records"].values()))["promoted"])

    async def test_one_promotion_per_day_and_counterevidence_demotes(self):
        for i in range(3):
            sid = self.observe(str(i), "求助先问清楚很好")
            await self.run_changes([self.change(sid, kind="trait", key=k, text=k+"求助先问清楚") for k in ("one", "two")])
            self.dream.recall("A", "P", "认真求助")
            self.dream.delivered("A", "P")
            if i < 2: self.now += 86400
        rows = self.store.read()["records"]
        self.assertEqual(sum(r["promoted"] for r in rows.values()), 1)
        accepted = next(r for r in rows.values() if r["promoted"])
        self.now += 21601
        sid = self.observe("against", "求助时你连续追问很烦，这次并不需要")
        c = self.change(sid, kind="trait", key=accepted["key"], text=accepted["text"])
        c["evidence"][0]["stance"] = "oppose"
        await self.run_changes([c])
        self.assertFalse(self.store.read()["records"][accepted["id"]]["promoted"])

    async def test_global_trait_exports_only_fixed_template_same_persona(self):
        for i in range(3):
            sid = self.observe(str(i), "求助先问清楚很好")
            await self.run_changes([self.change(sid, kind="trait", text="张三的私事带来求助经验", global_habit="clarify_first")])
            self.dream.recall("A", "P", "认真求助")
            self.dream.delivered("A", "P")
            if i < 2: self.now += 86400
        text = self.dream.recall("B", "P", "认真帮忙问题")
        self.assertIn("先弄清", text)
        self.assertNotIn("张三", text)
        self.assertEqual(self.dream.recall("B", "Q", "认真帮忙问题"), "")

    async def test_denied_native_mutation_does_not_forget(self):
        class Engine:
            async def delete_memory(self, memory_id): return False
        engine = Engine()
        gate = NativeGate()
        gate.wrap(engine, "delete_memory", self.dream, invalidates=True)
        self.observe()
        before = self.store.read()["epoch"]
        await engine.delete_memory(123)
        self.assertEqual(self.store.read()["epoch"], before)

    async def test_native_episode_merge_is_staged_after_new_write(self):
        sid = self.observe(text="还记得我们一起看鲸鱼吗")
        native = [dict(id="old-1", text="两位群友一起讨论了鲸鱼", fingerprint="v1", kind="episode", subject="")]
        async def memories(*args): return copy.deepcopy(native)
        self.adapter.memories = memories
        c = self.change(sid, kind="episode", text="群友讨论鲸鱼，后来再次聊起")
        c["evidence"].append(dict(source_id="native:old-1", quote="两位群友一起讨论了鲸鱼", stance="support"))
        c["native_supersedes"] = ["old-1"]
        await self.run_changes([c])
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertIn("native:old-1", self.adapter.archived)
        # Later summaries must retain the archived original's provenance.
        native.clear()
        self.now += 86400
        sid = self.observe("later", "后来我们又讨论鲸鱼")
        await self.run_changes([self.change(sid, kind="episode", text="后来又聊起鲸鱼")])
        self.dream.invalidate_native(["old-1"])
        self.assertFalse(any(r["status"] == "active" for r in self.store.read()["records"].values()))

    def test_native_memory_cannot_be_growth_evidence(self):
        native = [dict(id="old-1", text="看起来应该先问清楚", fingerprint="v1")]
        c = dict(kind="trait", key="clarify", text="先问清楚", context="求助", exceptions="简单问题", topics=["求助"],
                 evidence=[dict(source_id="native:old-1", quote="看起来应该先问清楚", stance="support")])
        with self.assertRaises(ValueError):
            parse_changes(json.dumps({"changes":[c]}), [], [], self.cfg, native)

    async def test_native_gate_reentrant_and_other_tasks_serialized(self):
        gate = NativeGate()
        events = []
        async def other():
            async with gate.enter(): events.append("other")
        async with gate.enter(internal=True):
            async with gate.enter(): self.assertTrue(gate.internal.get())
            task = asyncio.create_task(other())
            await asyncio.sleep(0)
            self.assertEqual(events, [])
        await task
        self.assertEqual(events, ["other"])


if __name__ == "__main__":
    unittest.main()

"""LivingMemory c486ed65 host adapter; no QQ sender is available to Dream."""
from __future__ import annotations

import asyncio
import json
import time

from .policy import digest, tokens
from .runtime import NativeGate, create, safe_observe, ensure_export_current, LOG

_runtime = None


def fingerprint(text, metadata):
    m = json.loads(metadata) if isinstance(metadata, str) else metadata
    return digest(text, {k:m.get(k) for k in ("session_id", "persona_id", "status", "subject", "canonical_summary", "persona_summary")})


class AstrAdapter:
    def __init__(self, plugin, session_check=None):
        self.plugin = plugin
        self.engine = plugin.initializer.memory_engine
        self.gate = NativeGate()
        self.manual_tasks = set()
        self.checkpoint_ready = False
        self.session_check = session_check or session_enabled

    async def allowed(self, profile):
        # Captures originate from LivingMemory after AstrBot's platform filters.
        return bool(self.plugin.initializer.is_initialized and not self.plugin._terminating
                    and getattr(self.plugin, "_whale_dream", None) is self.dream
                    and await self.session_check(profile["session"]))

    async def persona(self, profile):
        p = await self.plugin.context.persona_manager.get_persona(profile["persona"])
        return p.system_prompt

    async def generate(self, system, prompt):
        provider_id = self.plugin.config_manager.get("provider_settings.llm_provider_id", "")
        if not provider_id:
            raise RuntimeError("configure LivingMemory's llm_provider_id before enabling Dream")
        provider = self.plugin.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError("configured dream provider unavailable")
        response = await provider.text_chat(prompt=prompt, system_prompt=system, request_max_retries=1)
        return response.completion_text

    async def memories(self, profile, messages, limit):
        # Exact native scope SQL avoids optional recall filters / UUID fallbacks.
        cursor = await self.engine.db_connection.execute(
            "SELECT id,text,metadata FROM documents WHERE json_extract(metadata,'$.session_id')=? "
            "AND json_extract(metadata,'$.persona_id')=? AND COALESCE(json_extract(metadata,'$.status'),'active')='active' "
            "ORDER BY id DESC LIMIT ?", (profile["session"], profile["persona"], limit))
        rows = await cursor.fetchall()
        query = tokens(" ".join(m["text"] for m in messages))
        result = []
        for r in rows:
            metadata = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]
            result.append(dict(id=str(r["id"]), text=r["text"], fingerprint=fingerprint(r["text"], r["metadata"]),
                               kind=metadata.get("kind", "episode"), subject=metadata.get("subject", "")))
        result.sort(key=lambda r:len(query & tokens(r["text"])), reverse=True)
        return result

    async def unchanged(self, profile, memories):
        for m in memories:
            row = await (await self.engine.db_connection.execute("SELECT text,metadata FROM documents WHERE id=?", (int(m["id"]),))).fetchone()
            if not row or fingerprint(row["text"], row["metadata"]) != m["fingerprint"]:
                return False
        return True

    async def _find(self, external_id):
        row = await (await self.engine.db_connection.execute(
            "SELECT id FROM documents WHERE json_extract(metadata,'$.whale_dream_external_id')=? ORDER BY id LIMIT 1",
            (external_id,))).fetchone()
        return int(row["id"]) if row else None

    async def checkpoint(self):
        """Content-free fingerprints detect edits while the bridge is unloaded."""
        cursor = await self.engine.db_connection.execute("SELECT id,text,metadata FROM documents ORDER BY id")
        rows = {}
        try:
            while batch := await cursor.fetchmany(256):
                for row in batch:
                    rows[str(row["id"])] = fingerprint(row["text"], row["metadata"])
        finally:
            await cursor.close()
        return rows

    async def put(self, external_id, record, profile):
        async with self.gate.enter(internal=True):
            ensure_export_current(self.dream, record)
            old = await self._find(external_id)
            if old is not None:
                return str(old)
            metadata = dict(whale_dream_external_id=external_id, whale_dream_record=record["id"],
                whale_dream_version=record["version"], kind=record["kind"], subject=record["subject"],
                evidence_source=record["basis"], evidence_message_ids=[e["source_id"] for e in record["evidence"]],
                canonical_summary=record["text"], persona_summary=record["text"], summary_schema_version="v2")
            return str(await self.engine.add_memory(content=record["text"], session_id=profile["session"],
                persona_id=profile["persona"], importance=.6 if record["kind"] == "fact" else .5, metadata=metadata))

    async def remove(self, external_id, native_id):
        async with self.gate.enter(internal=True):
            target = int(native_id) if native_id else await self._find(external_id)
            if target is not None:
                await self.engine.archive_memories([target])


async def install(plugin):
    global _runtime
    if getattr(plugin, "_whale_dream", None) is not None or _runtime is not None:
        raise RuntimeError("dream runtime already installed; remove the legacy main.py hook")
    from astrbot.api.star import StarTools
    adapter = AstrAdapter(plugin)
    dream = create("astrbot", StarTools.get_data_dir("astrbot_plugin_livingmemory"), adapter)
    plugin._whale_dream = dream
    _runtime = dream
    try:
        hooks = adapter.gate.hooks
        # LivingMemory has no public mutation/capture subscription API yet.
        # Keep this version-pinned bridge reversible instead of editing main.py.
        async def no_periodic_consolidation(*args, **kwargs):
            return {"skipped": True, "reason": "whale_dream_managed"}

        async def manual_consolidation(*args, **kwargs):
            if getattr(plugin, "_whale_dream", None) is not dream:
                return {"skipped": True, "reason": "whale_dream_stopped"}
            task = asyncio.current_task()
            adapter.manual_tasks.add(task)
            try:
                return await dream.run_once()
            finally:
                adapter.manual_tasks.discard(task)

        hooks.replace(plugin.consolidation_manager, "maybe_run", no_periodic_consolidation)
        hooks.replace(plugin.consolidation_manager, "run_consolidation", manual_consolidation)
        adapter.gate.wrap(adapter.engine, "add_memory", dream)
        for name in ("update_memory", "delete_memory", "batch_delete_memories", "archive_memories"):
            adapter.gate.wrap(adapter.engine, name, dream, invalidates=True)
        manager = plugin.initializer.conversation_manager
        original = manager.add_message_from_event

        async def capture(event, role, content):
            result = await original(event=event, role=role, content=content)
            if getattr(plugin, "_whale_dream", None) is dream:
                try:
                    await observe(plugin, event, role, content)
                except Exception as exc:
                    LOG.error("dream status=capture_failed error_type=%s", type(exc).__name__)
            return result

        hooks.replace(manager, "add_message_from_event", capture)
        terminate = plugin.terminate

        async def stop_before_native_close():
            # on_plugin_unloaded fires AFTER terminate and database closure.
            plugin._terminating = True
            await shutdown(plugin)
            await terminate()

        hooks.replace(plugin, "terminate", stop_before_native_close)
        async with adapter.gate.enter():
            current = await adapter.checkpoint()
            previous = dream.store.read().get("astrbot_native_checkpoint")
            if previous:
                if previous["attached"]:
                    # No clean detach: we cannot prove which native changes were
                    # observed before a crash. Preserve the forgetting barrier.
                    dream.invalidate_native()
                else:
                    changed = [key for key, value in previous["rows"].items() if current.get(key) != value]
                    if changed:
                        dream.invalidate_native(changed)
            with dream.store.edit() as state:
                state["astrbot_native_checkpoint"] = {"attached": True, "rows": current}
            adapter.checkpoint_ready = True
        dream.start()
    except BaseException:
        await shutdown(plugin)
        raise


async def session_enabled(session):
    """Apply both plugins' per-session switches to passive capture and rounds."""
    from astrbot.api import sp
    try:
        services = await sp.get_async(scope="umo", scope_id=session, key="session_service_config", default={})
        if not isinstance(services, dict):
            return False
        enabled = services.get("session_enabled")
        if enabled is not None and not bool(enabled):
            return False
        plugins = await sp.get_async(scope="umo", scope_id=session, key="session_plugin_config", default={})
        disabled = plugins.get(session, {}).get("disabled_plugins", [])
        names = {"LivingMemory", "astrbot_plugin_livingmemory", "WhaleDream", "astrbot_plugin_whale_dream"}
        return isinstance(disabled, list) and not names.intersection(disabled)
    except Exception as exc:
        LOG.error("dream status=session_check_failed error_type=%s", type(exc).__name__)
        return False


def event_enabled(event):
    selected = getattr(event, "plugins_name", None)
    if selected is None or selected == ["*"]:
        return True
    return (bool({"LivingMemory", "astrbot_plugin_livingmemory"}.intersection(selected))
            and bool({"WhaleDream", "astrbot_plugin_whale_dream"}.intersection(selected)))


async def observe(plugin, event, role="user", content=None):
    dream = getattr(plugin, "_whale_dream", None)
    if dream is None or not event_enabled(event) or not await session_enabled(event.unified_msg_origin):
        return
    # Import the pinned plugin helper through its own package (not a guessed ID).
    import importlib
    package = plugin.__class__.__module__.rsplit(".", 1)[0]
    utils = importlib.import_module(package+".core.utils")
    persona = await utils.get_persona_id(plugin.context, event)
    if getattr(plugin, "_whale_dream", None) is not dream:
        return
    components = event.get_messages() or []
    # Own text stays eligible even when replying to somebody. Quoted/forwarded
    # components never become evidence attributed to the replying member.
    own_text = " ".join(str(getattr(c, "text", "")) for c in components if type(c).__name__ == "Plain")
    text = (content or "") if role == "assistant" else own_text or event.message_str
    sender = event.get_self_id() if role == "assistant" else event.get_sender_id()
    obj = event.message_obj
    message_id = str(getattr(obj, "message_id", "") or digest(event.unified_msg_origin, sender, text, getattr(obj, "timestamp", "")))
    quoted = not own_text and any(type(c).__name__ in {"Reply", "Forward", "Node", "Nodes"} for c in components)
    known_bot = bool(getattr(getattr(obj, "sender", None), "is_bot", False))
    safe_observe(dream.observe, session=event.unified_msg_origin, persona=persona,
        group=bool(event.get_group_id()), source_id=message_id+":"+role,
        sender=str(sender), text=str(text or ""), human=role == "user" and str(sender) != str(event.get_self_id()) and not known_bot,
        timestamp=time.time(), quoted=quoted)


async def inject(plugin, event, req):
    dream = getattr(plugin, "_whale_dream", None)
    if dream is None or not event_enabled(event):
        return
    try:
        await observe(plugin, event)
        import importlib
        utils = importlib.import_module(plugin.__class__.__module__.rsplit(".", 1)[0]+".core.utils")
        persona = await utils.get_persona_id(plugin.context, event)
        if (getattr(plugin, "_whale_dream", None) is not dream
                or not await session_enabled(event.unified_msg_origin)
                or getattr(plugin, "_whale_dream", None) is not dream):
            return
        req.system_prompt = (req.system_prompt or "")+dream.recall(event.unified_msg_origin, persona, event.message_str, str(event.get_sender_id()))
    except Exception as exc:
        LOG.error("dream status=injection_failed error_type=%s", type(exc).__name__)


async def shutdown(plugin):
    pending = getattr(plugin, "_whale_dream_shutdown", None)
    if pending is not None:
        await asyncio.shield(pending)
        return
    dream = getattr(plugin, "_whale_dream", None)
    if dream is None:
        return
    plugin._whale_dream = None
    # Both plugin unloading and native termination may arrive concurrently.
    # They must await the same drain before native storage can close.
    pending = asyncio.create_task(_shutdown_runtime(plugin, dream), name="whale-dream-shutdown")
    plugin._whale_dream_shutdown = pending
    await asyncio.shield(pending)


async def _shutdown_runtime(plugin, dream):
    global _runtime
    try:
        adapter = dream.adapter
        tasks = set(adapter.manual_tasks)
        if dream.task:
            tasks.add(dream.task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Drain admitted native mutations before closing the journal. Retained
        # wrapper references remain harmless after restoration.
        async with adapter.gate.enter():
            try:
                if adapter.checkpoint_ready:
                    rows = await adapter.checkpoint()
                    with dream.store.edit() as state:
                        state["astrbot_native_checkpoint"] = {"attached": False, "rows": rows}
            except Exception as exc:
                # Leave the dirty marker in place if native storage is broken.
                LOG.error("dream status=checkpoint_failed error_type=%s", type(exc).__name__)
            adapter.gate.active = False
            adapter.gate.hooks.restore()
            await dream.stop()
    finally:
        if _runtime is dream:
            _runtime = None
        plugin._whale_dream_shutdown = None


async def delivered(plugin, event):
    dream = getattr(plugin, "_whale_dream", None)
    if dream and event_enabled(event) and await session_enabled(event.unified_msg_origin):
        import importlib
        utils = importlib.import_module(plugin.__class__.__module__.rsplit(".", 1)[0]+".core.utils")
        persona = await utils.get_persona_id(plugin.context, event)
        if getattr(plugin, "_whale_dream", None) is dream:
            safe_observe(dream.delivered, event.unified_msg_origin, persona)

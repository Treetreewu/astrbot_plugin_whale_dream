"""MaiBot 2a9af62c A_Memorix adapter, installed in the existing host process."""
from __future__ import annotations

import json
import time

from .policy import digest, tokens
from .runtime import NativeGate, create, safe_observe, ensure_export_current, LOG

_runtime = None
PERSONA = "maibot-native-v1"


class MaiAdapter:
    def __init__(self, kernel):
        self.kernel = kernel
        self.gate = NativeGate()

    async def allowed(self, profile):
        return self.kernel.is_runtime_ready() and self.kernel.is_chat_enabled(stream_id=profile["session"])

    async def persona(self, profile):
        from src.config.config import global_config
        p = global_config.personality
        return "\n".join([p.personality, p.behavior_style, p.reply_style])

    async def generate(self, system, prompt):
        from src.services.llm_service import LLMServiceClient
        client = LLMServiceClient(task_name="memory", request_type="whale_dream")
        attempt = client._orchestrator._attempt_request_on_model
        async def once(api_provider, provider_client, request, retry_limit=None):
            return await attempt(api_provider, provider_client, request, retry_limit=1)
        client._orchestrator._attempt_request_on_model = once
        response = await client.generate_response(prompt=system+"\n\n"+prompt)
        return response.response

    async def memories(self, profile, messages, limit):
        # Read only exact chat sources; person-global profiles are intentionally
        # excluded because they can contain private-chat evidence.
        rows = self.kernel.metadata_store.get_paragraphs_by_source("chat_summary:"+profile["session"])
        query = tokens(" ".join(m["text"] for m in messages))
        result = []
        for r in rows:
            if r.get("is_deleted"):
                continue
            text = str(r.get("content", ""))
            meta = r.get("metadata") or {}
            if isinstance(meta, str): meta = json.loads(meta)
            result.append(dict(id=r["hash"], text=text, fingerprint=digest(text, r.get("metadata"), r.get("is_deleted")),
                               kind=meta.get("kind", "episode"), subject=meta.get("subject", "")))
        result.sort(key=lambda r:len(query & tokens(r["text"])), reverse=True)
        return result[:limit]

    async def unchanged(self, profile, memories):
        for m in memories:
            r = self.kernel.metadata_store.get_paragraph(m["id"])
            if not r or r.get("is_deleted") or digest(str(r.get("content", "")), r.get("metadata"), r.get("is_deleted")) != m["fingerprint"]:
                return False
        return True

    async def put(self, external_id, record, profile):
        async with self.gate.enter(internal=True):
            ensure_export_current(self.dream, record)
            old = self.kernel.metadata_store.get_external_memory_ref("whale_dream:"+external_id)
            if old:
                return old["paragraph_hash"]
            # Keep member facts in a chat-scoped paragraph, with subject and
            # explicit evidence metadata; do not feed cross-chat person profiles.
            result = await self.kernel.ingest_summary(external_id="whale_dream:"+external_id,
                chat_id=profile["session"], text=record["text"],
                metadata=dict(kind=record["kind"], subject=record["subject"], whale_dream_record=record["id"],
                    whale_dream_version=record["version"], evidence_source=record["basis"],
                    evidence_message_ids=[e["source_id"] for e in record["evidence"]]))
            if not result.get("success") or not result.get("stored_ids"):
                old = self.kernel.metadata_store.get_external_memory_ref("whale_dream:"+external_id)
                if not old:
                    raise RuntimeError("native dream write was not persisted")
                return old["paragraph_hash"]
            return str(result["stored_ids"][0])

    async def remove(self, external_id, native_id):
        async with self.gate.enter(internal=True):
            old = self.kernel.metadata_store.get_external_memory_ref("whale_dream:"+external_id)
            target = native_id or (old or {}).get("paragraph_hash")
            if target:
                result = await self.kernel.memory_delete_admin(action="execute", mode="paragraph",
                    selector={"hashes": [target]}, requested_by="whale_dream", reason="superseded_or_invalidated")
                if not result.get("success"):
                    raise RuntimeError("native dream archive failed")


def install(kernel):
    global _runtime
    if _runtime is not None:
        return
    adapter = MaiAdapter(kernel)
    dream = create("maibot", "/MaiMBot/data", adapter)
    _runtime = dream
    for name in ("ingest_summary", "ingest_text"):
        adapter.gate.wrap(kernel, name, dream)
    for name in ("memory_delete_admin", "memory_source_admin", "memory_correction_admin", "memory_fact_admin", "memory_profile_admin", "memory_v5_admin"):
        if hasattr(kernel, name):
            adapter.gate.wrap(kernel, name, dream, invalidates=True)
    dream.start()


def observe(message):
    if _runtime is None:
        return
    info = message.message_info
    user = info.user_info
    source = str(getattr(info, "message_id", "") or getattr(message, "message_id", ""))
    if not source or getattr(message, "is_command", False):
        return
    raw = getattr(message, "raw_message", None)
    components = getattr(raw, "components", []) or []
    own_text = " ".join(str(getattr(c, "text", "")) for c in components if type(c).__name__ == "TextComponent")
    quoted = not own_text and (bool(getattr(message, "reply_to", None)) or any("Forward" in type(c).__name__ or "Reply" in type(c).__name__ for c in components))
    received = getattr(message, "received_at", None)
    human = received is not None and not bool(getattr(user, "is_bot", False))
    safe_observe(_runtime.observe, session=str(message.session_id), persona=PERSONA,
        group=info.group_info is not None, source_id=source, sender=str(user.user_id),
        text=own_text or str(message.processed_plain_text or ""), human=human, timestamp=time.time(), quoted=quoted)
    if received is None:
        safe_observe(_runtime.delivered, str(message.session_id), PERSONA)


def inject(session):
    try:
        return _inject(session)
    except Exception as exc:
        LOG.error("dream status=injection_failed error_type=%s", type(exc).__name__)
        return ""


def _inject(session):
    if _runtime is None:
        return ""
    state = _runtime.store.read()
    scope = digest(session, PERSONA)
    recent = [m for m in state["sources"].values() if m["scope"] == scope and m.get("text") and m["human"]]
    if not recent:
        return ""
    latest = max(recent, key=lambda m:m["seq"])
    return _runtime.recall(session, PERSONA, latest["text"], latest["sender"])


async def shutdown():
    global _runtime
    if _runtime:
        await _runtime.stop()
        _runtime = None

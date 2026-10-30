"""AstrBot plugin lifecycle; the remaining LivingMemory bridge is version pinned."""
import asyncio

from . import astrbot
from .runtime import LOG


class DreamBinding:
    def __init__(self, context):
        self.context = context
        self.host = None
        self.lock = asyncio.Lock()
        self.watcher = None
        self.closed = False
        self.last_error = None

    def candidate(self):
        for name in ("astrbot_plugin_livingmemory", "LivingMemory"):
            meta = self.context.get_registered_star(name)
            if meta is None:
                continue
            if (not meta.activated or meta.version != "2.7.0-gd.12"
                    or meta.root_dir_name != "astrbot_plugin_livingmemory"):
                return None
            plugin = meta.star_cls
            if (plugin is not None and not plugin._terminating
                    and plugin.initializer.is_initialized
                    and plugin.consolidation_manager is not None
                    and plugin.event_handler is not None):
                return plugin
        return None

    async def reconcile(self):
        async with self.lock:
            if self.closed:
                return None
            try:
                candidate = self.candidate()
                if candidate is not self.host:
                    if self.host is not None:
                        await astrbot.shutdown(self.host)
                        self.host = None
                    if candidate is not None:
                        await astrbot.install(candidate)
                        self.host = candidate
                self.last_error = None
                return self.host
            except Exception as exc:
                # Retry delayed provider initialization without logging event data.
                error = type(exc).__name__
                if error != self.last_error:
                    LOG.error("dream status=binding_failed error_type=%s", error)
                    self.last_error = error
                return None

    async def start(self):
        if self.closed:
            return
        await self.reconcile()
        if self.watcher is None:
            self.watcher = asyncio.create_task(self.watch(), name="whale-dream-binding")

    async def watch(self):
        # LivingMemory may finish provider initialization after on_plugin_loaded.
        while not self.closed:
            await asyncio.sleep(1)
            await self.reconcile()

    async def request(self, event, req):
        plugin = await self.reconcile()
        if plugin is not None:
            await astrbot.inject(plugin, event, req)

    async def sent(self, event):
        plugin = await self.reconcile()
        if plugin is not None:
            try:
                await astrbot.delivered(plugin, event)
            except Exception as exc:
                LOG.error("dream status=delivery_failed error_type=%s", type(exc).__name__)

    async def stop(self):
        self.closed = True
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
            self.watcher = None
        async with self.lock:
            if self.host is not None:
                await astrbot.shutdown(self.host)
                self.host = None

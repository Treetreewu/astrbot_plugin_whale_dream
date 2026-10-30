"""Whale Dream entry point using AstrBot's public Star/filter APIs."""
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .whale_dream.astrbot_plugin import DreamBinding


class WhaleDreamPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.binding = DreamBinding(context)

    async def initialize(self):
        await self.binding.start()

    @filter.on_astrbot_loaded()
    async def ready(self):
        await self.binding.start()

    @filter.on_plugin_loaded()
    async def plugin_loaded(self, metadata):
        await self.binding.reconcile()

    @filter.on_plugin_unloaded()
    async def plugin_unloaded(self, metadata):
        await self.binding.reconcile()

    # LivingMemory's recall runs at priority 0. Inject after its request assembly.
    @filter.on_llm_request(priority=-100)
    async def request(self, event: AstrMessageEvent, req: ProviderRequest):
        await self.binding.request(event, req)

    @filter.after_message_sent(priority=-100)
    async def sent(self, event: AstrMessageEvent):
        await self.binding.sent(event)

    async def terminate(self):
        await self.binding.stop()

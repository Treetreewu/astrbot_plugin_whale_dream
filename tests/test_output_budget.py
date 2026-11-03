import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from whale_dream.maibot import MaiAdapter
from whale_dream.policy import TruncatedOutputError, reject_truncated_output, validate_config


class OutputBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_options_and_single_attempt_without_mutating_memory_task(self):
        service = ModuleType('src.services.llm_service')
        models = ModuleType('src.common.data_models.llm_service_data_models')
        models.LLMGenerationOptions = NS
        attempt = AsyncMock()
        native = NS(_orchestrator=NS(_attempt_request_on_model=attempt),
                    generate_response=AsyncMock(return_value=NS(response='{"changes":[]}',completion_tokens=500,provider_response={'choices':[{'finish_reason':'stop'}]})))
        service.LLMServiceClient=Mock(return_value=native)
        adapter=MaiAdapter(None)
        adapter.dream=NS(cfg={'output_tokens':16384})
        with patch.dict(sys.modules, {'src.services.llm_service':service,
             'src.common.data_models.llm_service_data_models':models}):
            self.assertEqual(await adapter.generate('system','prompt'),'{"changes":[]}')
            options=native.generate_response.call_args.kwargs['options']
            self.assertEqual(options.max_tokens,16384)
            await native._orchestrator._attempt_request_on_model('api','client','request',retry_limit=9)
            attempt.assert_awaited_once_with('api','client','request',retry_limit=1)
            native.generate_response.return_value.provider_response={'choices':[{'finish_reason':'length'}]}
            with self.assertRaises(TruncatedOutputError):await adapter.generate('system','prompt')

    def test_budget_bounds_and_legacy_config(self):
        cfg=json.loads((Path(__file__).resolve().parents[1]/'whale_dream/default_config.json').read_text())
        for bad in (True,4095,32769,'16384'):
            with self.assertRaises(ValueError):validate_config({**cfg,'output_tokens':bad})
        cfg.pop('output_tokens')
        validate_config(cfg)
        reject_truncated_output(None)
        with self.assertRaises(TruncatedOutputError):reject_truncated_output({'choices':[{'finish_reason':'length'}]})
        with self.assertRaises(TruncatedOutputError):reject_truncated_output(None,16384,16384)

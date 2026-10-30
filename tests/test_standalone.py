import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from whale_dream.astrbot import AstrAdapter
from whale_dream.runtime import config

ROOT = Path(__file__).resolve().parents[1]


class StandaloneTests(unittest.TestCase):
    def test_bundled_config_without_deployment_mount(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config()["rounds_per_day"], 2)

    def test_explicit_missing_config_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, WHALE_DREAM_CONFIG=str(Path(folder)/"missing.json")):
                with self.assertRaises(FileNotFoundError):
                    config()

    def test_entry_imports_its_own_package_from_unrelated_directory(self):
        # Minimal host API fixture; the pinned-upstream script checks actual
        # decorator bodies separately. No top-level whale_dream is on sys.path.
        code = r'''
import importlib
import sys
from types import ModuleType, SimpleNamespace
root = sys.argv[1]
sys.path.insert(0, root)
api = ModuleType("astrbot.api")
event = ModuleType("astrbot.api.event")
provider = ModuleType("astrbot.api.provider")
star = ModuleType("astrbot.api.star")
event.AstrMessageEvent = object
provider.ProviderRequest = object
star.Context = star.Star = object
def decorate(**kwargs):
    return lambda fn: fn
event.filter = SimpleNamespace(**{name: decorate for name in (
    "on_astrbot_loaded", "on_plugin_loaded", "on_plugin_unloaded", "on_llm_request", "after_message_sent")})
sys.modules.update({"astrbot": ModuleType("astrbot"), "astrbot.api": api,
    "astrbot.api.event": event, "astrbot.api.provider": provider, "astrbot.api.star": star})
entry = importlib.import_module(sys.argv[2]+".main")
assert entry.DreamBinding.__module__ == sys.argv[2]+".whale_dream.astrbot_plugin"
runtime = importlib.import_module(sys.argv[2]+".whale_dream.runtime")
assert runtime.config()["schema_version"] == 1
assert "whale_dream" not in sys.modules
'''
        with tempfile.TemporaryDirectory() as folder:
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "WHALE_DREAM_CONFIG"}}
            result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT.parent), ROOT.name],
                                    cwd=folder, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_rejects_modified_source_and_accepts_crlf(self):
        spec = importlib.util.spec_from_file_location("preflight", ROOT/"scripts/check-livingmemory.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root/"main.py"
            file.write_bytes(b"# pinned\r\n")
            lock = {"files": {"main.py": hashlib.sha256(b"# pinned\n").hexdigest()}}
            module.check(root, lock)
            file.write_text("# legacy dream hook\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from pin"):
                module.check(root, lock)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_provider_id_does_not_select_a_deployment_specific_model(self):
        plugin = SimpleNamespace(initializer=SimpleNamespace(memory_engine=None),
                                 config_manager=SimpleNamespace(get=lambda key, default: default))
        with self.assertRaisesRegex(RuntimeError, "llm_provider_id"):
            adapter = object.__new__(AstrAdapter)
            adapter.plugin = plugin
            await adapter.generate("system", "prompt")


if __name__ == "__main__":
    unittest.main()

import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
import tempfile
from unittest.mock import Mock, patch

from whale_dream import maibot
from whale_dream.identity import is_human_sender
from whale_dream.policy import validate_config
from whale_dream.runtime import create


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((Path(__file__).parents[1]/"whale_dream/default_config.json").read_text())
        self.cfg["sender_roles"] = {"maibot": {"session": {"human-id": "human", "bot-id": "bot"}}}

    def test_exact_trusted_roles_flags_and_unknown(self):
        for sender, flags, expected in (("unknown", (), False), ("unknown", (False,), True),
                ("unknown", ("false", 0), False), ("human-id", (), True),
                ("human-id", (True,), False), ("bot-id", (False,), False)):
            with self.subTest(sender=sender, flags=flags):
                self.assertEqual(is_human_sender(self.cfg, "maibot", "session", sender, flags=flags), expected)
        self.assertFalse(is_human_sender(self.cfg, "maibot", "other-session", "human-id"))
        self.assertFalse(is_human_sender(self.cfg, "astrbot", "session", "human-id"))
        self.assertFalse(is_human_sender(self.cfg, "maibot", "session", "human-id", self_id="human-id"))

    def test_role_config_validation(self):
        validate_config(self.cfg)
        for roles in ([], {"unknown": {}}, {"maibot": []}, {"maibot": {"s": {"u": "nickname"}}}):
            bad = {**copy.deepcopy(self.cfg), "sender_roles": roles}
            with self.assertRaises(ValueError):
                validate_config(bad)

    def test_private_mapping_is_loaded_without_changing_source_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"whale-dream/sender-roles.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"session": {"trusted-id": "human"}}), encoding="utf-8")
            with patch("whale_dream.runtime.config", return_value=self.cfg):
                dream = create("maibot", folder, NS())
            try:
                self.assertTrue(is_human_sender(dream.cfg, "maibot", "session", "trusted-id"))
                self.assertNotIn("trusted-id", self.cfg["sender_roles"]["maibot"]["session"])
            finally:
                dream.store.close()

    def test_maibot_native_sender_without_bot_field_uses_transport_or_trusted_id(self):
        runtime = NS(cfg=self.cfg, observe=Mock(), delivered=Mock())
        text = type("TextComponent", (), {})()
        text.text = "test own text"
        user = NS(user_id="unknown", user_nickname="真人昵称", user_cardname=None)
        info = NS(user_info=user, group_info=NS(), additional_config={})
        message = NS(message_id="m", message_info=info, session_id="session", account_id="self",
                     raw_message=NS(components=[text]), received_at=1, processed_plain_text=text.text)
        with patch.object(maibot, "_runtime", runtime):
            maibot.observe(message)
            self.assertFalse(runtime.observe.call_args.kwargs["human"])
            info.additional_config["qq_official_sender_is_bot"] = False
            maibot.observe(message)
            self.assertTrue(runtime.observe.call_args.kwargs["human"])
            user.user_id = "bot-id"
            maibot.observe(message)
            self.assertFalse(runtime.observe.call_args.kwargs["human"])

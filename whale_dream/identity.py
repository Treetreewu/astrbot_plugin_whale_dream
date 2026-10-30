"""Evidence identity comes from transport flags or exact operator-owned IDs."""


def is_human_sender(cfg, bot, session, sender, *, self_id="", flags=()):
    role = cfg.get("sender_roles", {}).get(bot, {}).get(session, {}).get(sender)
    # Positive bot identification always wins, including over a stale role map.
    if not sender or (self_id and sender == self_id) or role == "bot" or any(v is True for v in flags):
        return False
    return role == "human" or any(v is False for v in flags)

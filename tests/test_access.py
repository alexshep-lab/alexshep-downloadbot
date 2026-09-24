import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot

OWNER = 213925600
STRANGER = 555
GROUP = -100777


def run(coro):
    return asyncio.run(coro)


def fake_message(chat_id, chat_type, user_id, *, title="Группа", text="https://youtu.be/abc"):
    user = SimpleNamespace(id=user_id, full_name="Вася <script>", username="vasya")
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=title, username=None)
    tg = SimpleNamespace(send_message=AsyncMock(), get_chat_member_count=AsyncMock(return_value=42))
    return SimpleNamespace(chat=chat, from_user=user, sender_chat=None, text=text, bot=tg, reply=AsyncMock())


class AccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, value in {
            "ACCESS_FILE": Path(temp.name) / "access.json",
            "access": {"chats": {}, "users": {}},
            "access_reminded": {},
            "ALLOWED_USER_IDS": {OWNER},
            "ALLOWED_CHAT_IDS": {-100111},
        }.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_stranger_in_private_gets_one_request_to_owner(self):
        msg = fake_message(STRANGER, bot.ChatType.PRIVATE, STRANGER)
        self.assertFalse(bot.is_allowed(msg))
        run(bot.ask_stranger(msg))
        run(bot.ask_stranger(msg))  # повтор не должен слать владельцу второй запрос
        owner_calls = [c for c in msg.bot.send_message.call_args_list if c.args[0] == OWNER]
        self.assertEqual(len(owner_calls), 1)
        self.assertIn("&lt;script&gt;", owner_calls[0].args[1])  # имя экранировано
        self.assertIn("@AlexShep", msg.reply.call_args_list[0].args[0])
        # на второе сообщение подряд не отвечаем: раз в ACCESS_REMIND_SEC, чтобы не упереться в лимиты
        self.assertEqual(msg.reply.await_count, 1)
        self.assertEqual(bot.access_status("users", STRANGER), "pending")

    def test_decision_opens_access_and_can_be_changed(self):
        msg = fake_message(STRANGER, bot.ChatType.PRIVATE, STRANGER)
        run(bot.ask_stranger(msg))
        run(bot.apply_decision(msg.bot, "users", STRANGER, "allowed"))
        self.assertTrue(bot.is_allowed(msg))
        run(bot.apply_decision(msg.bot, "users", STRANGER, "denied"))
        self.assertFalse(bot.is_allowed(msg))
        # решение переживает перезапуск
        saved = bot.access
        with patch.object(bot, "access", {"chats": {}, "users": {}}):
            bot.load_access()
            self.assertEqual(bot.access_status("users", STRANGER), "denied")
        self.assertIs(bot.access, saved)

    def test_group_request_reminds_rarely_and_approval_covers_everyone(self):
        msg = fake_message(GROUP, bot.ChatType.SUPERGROUP, STRANGER)
        run(bot.ask_for_group(msg))
        run(bot.ask_for_group(msg))
        self.assertEqual(msg.reply.await_count, 1)  # второе напоминание — не раньше чем через 10 минут
        owner_text = msg.bot.send_message.call_args_list[0].args[1]
        self.assertIn("Участников: 42", owner_text)
        run(bot.apply_decision(msg.bot, "chats", GROUP, "allowed"))
        other = fake_message(GROUP, bot.ChatType.SUPERGROUP, 999)
        self.assertTrue(bot.is_allowed(other))

    def test_env_group_can_be_revoked(self):
        msg = fake_message(-100111, bot.ChatType.SUPERGROUP, STRANGER)
        self.assertTrue(bot.is_allowed(msg))
        run(bot.apply_decision(msg.bot, "chats", -100111, "denied"))
        self.assertFalse(bot.is_allowed(msg))

    def test_access_list_has_buttons_for_every_entry(self):
        bot.set_access("users", STRANGER, "pending", "Вася")
        text, keyboard = bot.access_list()
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"acl:u:{STRANGER}:y", data)
        self.assertIn("acl:c:-100111:n", data)  # группа из .env тоже управляется
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))


class AccessLimitsTests(AccessTests):
    """Защита от спама: лимит ожидающих запросов, ленивое описание, битый access.json."""

    def test_pending_cap_stops_new_requests_and_notifications(self):
        with patch.object(bot, "MAX_PENDING_ACCESS", 2):
            for user_id in (1001, 1002, 1003):
                run(bot.ask_stranger(fake_message(user_id, bot.ChatType.PRIVATE, user_id)))
        self.assertEqual(bot.access_status("users", 1003), None)
        self.assertEqual(sum(r["status"] == "pending" for r in bot.access["users"].values()), 2)

    def test_group_details_fetched_only_for_new_request(self):
        msg = fake_message(GROUP, bot.ChatType.SUPERGROUP, STRANGER)
        run(bot.ask_for_group(msg))
        run(bot.ask_for_group(msg))
        self.assertEqual(msg.bot.get_chat_member_count.await_count, 1)

    def test_malformed_access_file_is_ignored(self):
        for payload in ("[]", '{"chats": []}', '{"users": {"x": 1, "5": "bad", "7": {"status": "allowed"}}}'):
            bot.ACCESS_FILE.write_text(payload, encoding="utf-8")
            with patch.object(bot, "access", {"chats": {}, "users": {}}):
                bot.load_access()  # не должно падать
                self.assertEqual(list(bot.access["users"]), ["7"] if "7" in payload else [])


if __name__ == "__main__":
    unittest.main()

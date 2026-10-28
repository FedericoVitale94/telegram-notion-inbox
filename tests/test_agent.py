import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_INBOX_DB_ID", "inbox123")
os.environ.setdefault("NOTION_TOKEN", "x")
import agent  # noqa: E402
import config  # noqa: E402
import telegram  # noqa: E402


def page(title, category, topic="", pid=None, author="", minutes=None):
    p = {"Title": {"type": "title", "title": [{"plain_text": title}]},
         "Category": {"type": "select", "select": {"name": category}},
         "Status": {"type": "select", "select": {"name": "To review"}},
         "Saved": {"type": "date", "date": {"start": "2026-10-28T10:00:00+00:00"}},
         "Topic": {"type": "rich_text", "rich_text": [{"plain_text": topic}]},
         "Author": {"type": "rich_text", "rich_text": [{"plain_text": author}] if author else []},
         "Reading time (min)": {"type": "number", "number": minutes}}
    return {"id": pid or title.lower(), "url": f"https://notion.so/{title}", "properties": p}


def final_message(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, f in (("HISTORY", "history.json"), ("USAGE", "usage.json")):
            p = mock.patch.object(agent, name, Path(self.tmp.name) / f)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        agent._drafts_written = 0

    def test_routing(self):
        self.assertTrue(agent.is_question({"text": "? articles under 10 minutes"}))
        self.assertTrue(agent.is_question({"text": "/ask how many books"}))
        self.assertFalse(agent.is_question({"text": "buy batteries?"}))
        self.assertFalse(agent.is_question({"text": "/asking"}))
        self.assertFalse(agent.is_question({"caption": "? photo", "photo": [{}]}))
        with mock.patch.dict(config.AGENT, {"enabled": False}):
            self.assertFalse(agent.is_question({"text": "? anything"}))

    def test_history_per_chat_and_expiry(self):
        agent.remember_exchange(1, "q1", "a1")
        agent.remember_exchange(2, "q2", "a2")
        self.assertEqual([m["content"] for m in agent.history(1)], ["q1", "a1"])
        with mock.patch("time.time", return_value=time.time() + 3 * 3600):
            self.assertEqual(agent.history(1), [])

    def test_search_inbox_filters_and_ranks(self):
        rows = [page("Cake", "recipe"), page("The Name of the Rose", "book", author="Umberto Eco"),
                page("Eco and semiotics", "article", minutes=12)]
        with mock.patch("notion.mirror", return_value=rows):
            out = json.loads(agent.search_inbox.call({"text": "umberto eco", "category": "book"}))
        self.assertEqual([p["title"] for p in out["pages"]], ["The Name of the Rose"])
        self.assertEqual(out["pages"][0]["author"], "Umberto Eco")

    def test_read_page_cut(self):
        with mock.patch("notion.page", return_value=page("A", "article")), \
                mock.patch("notion.page_sections", return_value={"Summary": "x" * 20000}):
            self.assertIn("[text cut]", agent.read_page.call({"page_id": "p"}))

    def test_bulk_status_needs_confirmation(self):
        with mock.patch("notion.update_inbox_page") as up:
            out = agent.set_status.call({"page_ids": ["a", "b", "c", "d"], "status": "Discarded"})
        self.assertIn("confirmation", out)
        up.assert_not_called()

    def test_invalid_status(self):
        self.assertIn("invalid", agent.set_status.call({"page_ids": ["a"], "status": "Maybe"}))

    def test_creator_tools_only_in_creator_mode(self):
        names = lambda: [getattr(t, "name", None) or t["name"] for t in agent.tools()]  # noqa: E731
        self.assertNotIn("write_draft", names())
        self.assertIn("web_search", names())
        with mock.patch.object(config, "CREATOR_ON", True), mock.patch.dict(config.ARCHIVE, {"database_id": "x"}):
            self.assertIn("write_draft", names())
            self.assertIn("search_published", names())
        with mock.patch.dict(config.AGENT, {"web_searches": 0}):
            self.assertNotIn("web_search", names())

    def test_at_most_two_drafts(self):
        with mock.patch("notion.page", return_value={}), mock.patch("pipeline.draft_for_page", return_value="ok") as d:
            for _ in range(3):
                out = agent.write_draft.call({"page_id": "p"})
        self.assertEqual(d.call_count, 2)
        self.assertIn("limit", out)

    def test_answer_uses_history(self):
        with mock.patch("agent._runner", return_value=[final_message("You have 3 books to read.")]) as r:
            self.assertEqual(agent.answer({"chat": {"id": 1}, "text": "? books"}), "You have 3 books to read.")
        self.assertEqual(r.call_args[0][0][-1], {"role": "user", "content": "books"})
        self.assertEqual(agent.history(1)[-1]["content"], "You have 3 books to read.")

    def test_not_finished(self):
        with mock.patch("agent._runner", return_value=[final_message("partial", stop="tool_use")]):
            self.assertIn("not finished", agent.answer({"chat": {"id": 1}, "text": "? x"}))

    def test_daily_limit(self):
        with mock.patch("agent._used_today", return_value=30), mock.patch("agent._runner") as r:
            self.assertIn("limit", agent.answer({"chat": {"id": 1}, "text": "? x"}))
        r.assert_not_called()

    def test_link_with_question_mark(self):
        with mock.patch("agent._runner") as r:
            self.assertIn("without the question mark", agent.answer({"chat": {"id": 1}, "text": "? save https://x.com/a"}))
        r.assert_not_called()

    def test_prompt_uses_config(self):
        p = agent.system_prompt()
        self.assertIn(config.BRAND, p)
        self.assertIn("To review", p)
        self.assertNotIn("Creator mode is on", p)


class TestTelegram(unittest.TestCase):
    def test_long_message_in_pieces(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
        with mock.patch("telegram._call") as call:
            telegram.reply(1, text, reply_to=5)
        sent = [c.kwargs["text"] for c in call.call_args_list]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(p) <= 4096 for p in sent))
        self.assertIn("reply_parameters", call.call_args_list[0].kwargs)
        self.assertNotIn("reply_parameters", call.call_args_list[1].kwargs)
        self.assertEqual("\n".join(sent), text)


class TestDue(unittest.TestCase):
    def test_due(self):
        import main
        with tempfile.TemporaryDirectory() as d:
            mark = Path(d) / "mark"
            self.assertTrue(main.due(mark, 1))
            mark.touch()
            self.assertFalse(main.due(mark, 1))
            old = time.time() - 2 * 3600
            os.utime(mark, (old, old))
            self.assertTrue(main.due(mark, 1))


if __name__ == "__main__":
    unittest.main()

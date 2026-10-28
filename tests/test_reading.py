import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_INBOX_DB_ID", "inbox123")
os.environ.setdefault("NOTION_TOKEN", "x")
import config  # noqa: E402
import ideas  # noqa: E402
import notion  # noqa: E402
import pipeline  # noqa: E402

PAGE_URL = "https://www.notion.so/Page-0123456789abcdef0123456789abcdef"


def classified(**kw):
    c = {"category": "other", "title": "Some title", "topic": "", "tags": [], "summary": "",
         "book_title": "", "book_author": ""}
    c.update(kw)
    return c


class TestReading(unittest.TestCase):
    def test_article_words_skip_menus_and_scripts(self):
        page = ("<nav>" + "menu " * 500 + "</nav><script>var a = 1;</script>"
                "<article><h1>Title</h1><p>" + "word " * 120 + "</p></article><footer>" + "x " * 400 + "</footer>")
        self.assertEqual(pipeline.article_words(page), 121)

    def test_article_words_paragraphs_without_article(self):
        self.assertEqual(pipeline.article_words("<div>skip me</div><p>one two</p><p class='x'>three</p>"), 3)

    def test_reading_minutes(self):
        self.assertIsNone(pipeline.reading_minutes(120))
        self.assertEqual(pipeline.reading_minutes(2200), 10)
        self.assertEqual(pipeline.reading_minutes(300), 1)

    def test_creator_mode_off_by_default(self):
        self.assertFalse(config.CREATOR_ON)
        self.assertNotIn(config.IDEA, config.CATEGORIES)
        self.assertEqual(config.BOOK, "book")

    def _process(self, c, words=0, text="https://example.com/page"):
        msg = {"message_id": 7, "date": 0, "chat": {"id": 1}, "text": text}
        with mock.patch("notion.find_by_url", return_value=None), \
                mock.patch("pipeline.web_page", return_value={"preview": "Page title: x", "words": words}), \
                mock.patch("pipeline.classify", return_value=c), \
                mock.patch("notion.create_inbox_page", return_value=PAGE_URL) as create, \
                mock.patch("books.lookup", return_value=None), \
                mock.patch("ideas.analyze") as analyze:
            result = pipeline.process(msg)
        return result["reply"], create.call_args[0][0], analyze

    def test_book_gets_its_title_and_author(self):
        reply, props, _ = self._process(classified(category="book", title="A classic novel",
                                                   book_title="The Name of the Rose", book_author="Umberto Eco"))
        self.assertEqual(props["Title"]["title"][0]["text"]["content"], "The Name of the Rose")
        self.assertEqual(props["Author"]["rich_text"][0]["text"]["content"], "Umberto Eco")
        self.assertIn("Umberto Eco", reply)

    def test_article_gets_reading_time(self):
        reply, props, _ = self._process(classified(category="article"), words=2200)
        self.assertEqual(props["Reading time (min)"]["number"], 10)
        self.assertIn("10 min", reply)
        self.assertNotIn("Book", props)

    def test_short_page_has_no_reading_time(self):
        _, props, _ = self._process(classified(category="other"), words=80)
        self.assertNotIn("Reading time (min)", props)

    def test_no_idea_analysis_without_creator_mode(self):
        with mock.patch.object(config, "CATEGORIES", config.CATEGORIES + ["idea"]):
            _, props, analyze = self._process(classified(category="idea"), text="some idea")
        analyze.assert_not_called()
        self.assertNotIn("Idea score", props)

    def test_ensure_properties_adds_only_missing(self):
        have = {"properties": {"Title": {}, "Book": {}}}
        with mock.patch("net.request") as req:
            req.return_value.json.return_value = have
            added = notion.ensure_properties(creator=False)
        self.assertEqual(added, ["Author", "Reading time (min)", "Telegram chat ID"])
        self.assertEqual(req.call_args[0][0], "PATCH")
        self.assertEqual(set(req.call_args[1]["json"]["properties"]), {"Author", "Reading time (min)", "Telegram chat ID"})


if __name__ == "__main__":
    unittest.main()

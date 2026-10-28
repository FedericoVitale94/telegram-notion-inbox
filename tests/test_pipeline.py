import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_INBOX_DB_ID", "inbox123")
os.environ.setdefault("NOTION_TOKEN", "x")
import drafts  # noqa: E402
import ideas  # noqa: E402
import media  # noqa: E402
import notion  # noqa: E402
import pipeline  # noqa: E402


def update(uid, mid, date=100, **msg):
    return {"update_id": uid, "message": {"message_id": mid, "date": date, "chat": {"id": 1}, **msg}}


class TestPipeline(unittest.TestCase):
    def test_extract_urls_from_text_and_entities(self):
        msg = {"message_id": 7, "date": 1761552000,
               "text": "look (https://www.instagram.com/reel/abc/?igsh=x). and https://vm.tiktok.com/ZZ/",
               "entities": [{"type": "text_link", "url": "https://example.com/a"}],
               "forward_origin": {"type": "channel", "chat": {"title": "Channel"}}}
        item = pipeline.extract(msg)
        self.assertEqual(item["urls"], ["https://www.instagram.com/reel/abc/?igsh=x",
                                        "https://vm.tiktok.com/ZZ/", "https://example.com/a"])
        self.assertEqual((item["kind"], item["origin"]), ("link, forwarded", "Channel"))

    def test_media_kind(self):
        item = pipeline.extract({"message_id": 1, "date": 0, "photo": [{}], "caption": "note"})
        self.assertEqual((item["text"], item["urls"], item["kind"]), ("note", [], "photo"))
        self.assertEqual(pipeline.extract({"message_id": 1, "date": 0, "document": {}})["kind"], "file")

    def test_platform(self):
        cases = {"https://www.instagram.com/reel/x": "Instagram", "https://vm.tiktok.com/x": "TikTok",
                 "https://youtu.be/x": "YouTube", "https://m.youtube.com/shorts/x": "YouTube",
                 "https://notinstagram.com.evil.example/x": "Web", "mailto:a@b.it": "Web", None: "Text"}
        for url, expected in cases.items():
            self.assertEqual(pipeline.platform(url), expected, url)

    def test_merge_split_long_text(self):
        long = "a" * 4000
        out = pipeline.merge_split([update(1, 10, text=long), update(2, 11, text="end"),
                                    update(3, 12, text="another subject")])
        self.assertEqual([(m["message_id"], uid) for m, uid in out], [(10, 2), (12, 3)])
        self.assertEqual(out[0][0]["text"], long + "\nend")
        # short messages sent together, or forwarded ones, stay separate
        self.assertEqual(len(pipeline.merge_split([update(1, 10, text="one"), update(2, 11, text="two")])), 2)
        fwd = {"forward_origin": {"type": "channel", "chat": {"title": "C"}}}
        self.assertEqual(len(pipeline.merge_split([update(1, 10, text=long, **fwd),
                                                   update(2, 11, text=long, **fwd)])), 2)

    def test_merge_album(self):
        album = [update(i, 38 + i, media_group_id="g1", photo=[{}]) for i in range(3)]
        album[0]["message"]["caption"] = "caption on the first photo"
        out = pipeline.merge_split(album + [update(9, 50, media_group_id="g2", photo=[{}])])
        self.assertEqual([uid for _, uid in out], [2, 9])
        item = pipeline.extract(out[0][0])
        self.assertEqual((item["text"], item["kind"]), ("caption on the first photo", "album of 3"))

    def test_chunks_respect_notion_limit(self):
        blocks = notion.paragraphs("x" * 4500)
        self.assertEqual(len(blocks), 3)
        text = "💀" + "x" * 1999  # 2001 UTF-16 units: the case Notion rejected
        parts = notion.chunks(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 2000 for p in parts))

    def test_whisper_phantom_phrases(self):
        for t in ["Thank you.", "The Thank you.", "The End", "Sottotitoli a cura di QTSS"]:
            self.assertTrue(media.PHANTOM.fullmatch(t), t)
        self.assertFalse(media.PHANTOM.fullmatch("Thank you for the story about the witch"))

    def test_text_neighbours(self):
        entries = [{"title": t, "text": "", "script": ""}
                   for t in ["Black holes explained", "The first vaccine", "Robots in factories"]]
        self.assertEqual(ideas.neighbours("why black holes evaporate", entries)[0]["title"], "Black holes explained")
        self.assertEqual(ideas.neighbours("chocolate cake recipe", entries), [])

    def test_draft_word_checks(self):
        self.assertEqual(drafts.checks("word " * 190, 160, 220), [])
        self.assertEqual(drafts.checks("too short", 160, 220), ["2 words (target 160-220)"])

    def test_forced_category(self):
        self.assertEqual(pipeline.forced_category("https://x.example #recipe"), "recipe")
        self.assertEqual(pipeline.forced_category("#PersonalNote buy milk"), "personal_note")
        self.assertIsNone(pipeline.forced_category("#space #nasa"))

    def test_write_guard(self):
        with self.assertRaises(PermissionError):
            notion._append("some-other-page", [])
        other_db_page = mock.Mock(**{"json.return_value": {"id": "p1", "parent": {"database_id": "other-db"}}})
        with mock.patch("net.request", return_value=other_db_page) as req, self.assertRaises(PermissionError):
            notion.append_to_inbox_page("p1", [])
        self.assertEqual(req.call_count, 1)  # only the read, no write


if __name__ == "__main__":
    unittest.main()

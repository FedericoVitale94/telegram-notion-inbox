"""The bot run on an Italian instance (tests/fixtures/istanza_it): property names, statuses and headings from the
config, a forced category with aliases and a topic, creator mode with custom.py, deleting processed messages,
attachments, books, spending. It checks that a personal instance loses nothing the bot can do."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_INBOX_DB_ID", "inbox123")
os.environ.setdefault("NOTION_TOKEN", "x")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "42")
import agent  # noqa: E402
import books  # noqa: E402
import config  # noqa: E402
import costs  # noqa: E402
import destinations  # noqa: E402
import drafts  # noqa: E402
import i18n  # noqa: E402
import ideas  # noqa: E402
import main  # noqa: E402
import notion  # noqa: E402
import pipeline  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "istanza_it"
PAGE_URL = "https://www.notion.so/Pagina-0123456789abcdef0123456789abcdef"
PRIVATA = {"id": 42, "type": "private"}
GRUPPO = {"id": -100123, "type": "supergroup"}


def msg(chat=PRIVATA, mid=7, thread=None, **kw):
    m = {"message_id": mid, "chat": chat, "date": 0, "from": {"id": 42}, **kw}
    if thread is not None:
        m["message_thread_id"] = thread
    return m


def classified(**kw):
    c = {"category": "altro", "title": "Un titolo", "topic": "", "tags": [], "summary": "",
         "book_title": "", "book_author": ""}
    c.update(kw)
    return c


class Istanza(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "istanza"
        shutil.copytree(FIXTURE, cls.root)
        cls.old_root = config.ROOT
        config.ROOT = cls.root
        config.load(cls.root / "config.toml")

    @classmethod
    def tearDownClass(cls):
        config.ROOT = cls.old_root
        config.load()
        cls.tmp.cleanup()

    def setUp(self):
        shutil.rmtree(self.root / "cache", ignore_errors=True)
        for p in (mock.patch("costs.usd_per_eur", return_value=1.10),
                  mock.patch.object(ideas, "_inbox", [])):
            p.start()
            self.addCleanup(p.stop)

    def process(self, m, c=None, words=0, lookup=None, analysis=None, over_limit=False):
        with mock.patch("notion.find_by_url", return_value=None), \
                mock.patch("pipeline.web_page", return_value={"preview": "", "words": words}), \
                mock.patch("pipeline.classify", return_value=c or classified()) as cl, \
                mock.patch("media.video") as video, \
                mock.patch("books.lookup", return_value=lookup) as lk, \
                mock.patch("ideas.analyze", return_value=analysis) as an, \
                mock.patch("drafts.draft", return_value={"draft": "Sapevi che", "to_verify": [], "checks": []}), \
                mock.patch("costs.over_limit", return_value=over_limit), \
                mock.patch("notion.create_inbox_page", return_value=PAGE_URL) as create:
            result = pipeline.process(m)
        self.calls = SimpleNamespace(classify=cl, video=video, lookup=lk, analyze=an, create=create)
        return result


class TestNomiETesti(Istanza):
    def test_proprieta_stati_e_titoli_dal_config(self):
        r = self.process(msg(text="una nota qualsiasi"), classified(summary="Riassunto di prova"))
        props, blocks = self.calls.create.call_args[0][:2]
        for nome in ("Titolo", "Categoria", "Piattaforma", "Data salvataggio", "Stato", "Argomento", "Filone",
                     "Telegram message ID", "Telegram chat ID", "Errore"):
            self.assertIn(nome, props)
        self.assertEqual(props["Stato"]["select"]["name"], "Da vedere")
        self.assertEqual(props["Piattaforma"]["select"]["name"], "Testo")
        titoli = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] == "heading_2"]
        self.assertEqual(titoli, ["Testo originale", "Riassunto"])
        self.assertTrue(blocks[0]["paragraph"]["rich_text"][0]["text"]["content"].startswith("Tipo: testo"))
        self.assertTrue(r["deletable"])

    def test_stringa_sostituita_dal_config(self):
        self.assertEqual(i18n.t("checks_ok"), "Tutto ok: parole e apertura.")
        self.assertEqual(i18n.t("sec_draft"), "Bozza script")

    def test_prompt_sostituito_da_file(self):
        with mock.patch("llm.complete_json", return_value={"category": "altro"}) as cj:
            pipeline.classify({"urls": [], "context": "testo"}, "Testo")
        self.assertTrue(cj.call_args[0][0].startswith("PROMPT DI PROVA"))
        self.assertNotIn("idea_bot", cj.call_args[0][1]["properties"]["category"]["enum"])


class TestCategorieForzate(Istanza):
    def test_hashtag_e_alias(self):
        self.assertEqual(pipeline.forced_category("#migliorie usare i topic"), "idea_bot")
        self.assertEqual(pipeline.forced_category("#IdeaBot x"), "idea_bot")
        self.assertEqual(pipeline.forced_category("#nota comprare pile"), "nota_personale")
        self.assertEqual(pipeline.forced_category("#libro"), "libro")
        self.assertIsNone(pipeline.forced_category("#horror"))

    def test_nota_forzata_senza_analisi(self):
        r = self.process(msg(text="#bot\nriepilogo mensile delle bozze https://www.tiktok.com/@x/video/1"))
        self.calls.classify.assert_not_called()
        self.calls.video.assert_not_called()
        props = self.calls.create.call_args[0][0]
        self.assertEqual(props["Categoria"]["select"]["name"], "idea_bot")
        self.assertEqual(props["Titolo"]["title"][0]["text"]["content"], "riepilogo mensile delle bozze https://www.tiktok.com/@x/video/1")
        self.assertTrue(r["deletable"])

    def test_topic_forzato(self):
        self.assertIn("idea_bot", destinations.topic_command(msg(GRUPPO, thread=7, text="/qui bot")))
        self.assertIn("salvati", destinations.topic_command(msg(GRUPPO, thread=5, text="/qui@inbox_bot salvati")))
        self.assertIn("Generale", destinations.topic_command(msg(GRUPPO, text="/qui avvisi")))
        self.assertIn("Non riconosco", destinations.topic_command(msg(GRUPPO, thread=9, text="/qui boh")))
        self.assertIsNone(destinations.topic_command(msg(PRIVATA, text="/qui salvati")))
        self.assertEqual(destinations.topic_category(msg(GRUPPO, thread=7, text="x")), "idea_bot")
        self.assertIsNone(destinations.topic_category(msg(GRUPPO, thread=8, text="x")))
        self.assertTrue(destinations.ignore(msg(GRUPPO, thread=5, text="x")))
        self.assertFalse(destinations.ignore(msg(GRUPPO, thread=7, text="x")))
        self.process(msg(GRUPPO, thread=7, text="usare i topic anche per le ricette"))
        self.calls.classify.assert_not_called()
        self.assertEqual(self.calls.create.call_args[0][0]["Categoria"]["select"]["name"], "idea_bot")

    def test_topic_cambia_ruolo(self):
        destinations.topic_command(msg(GRUPPO, thread=5, text="/qui salvati"))
        destinations.topic_command(msg(GRUPPO, thread=5, text="/qui ricetta"))
        self.assertEqual(destinations.where("saved")[1], None)  # non è più il topic delle risposte
        self.assertEqual(destinations.topic_category(msg(GRUPPO, thread=5, text="x")), "ricetta")

    def test_risposte_nei_topic(self):
        destinations.topic_command(msg(GRUPPO, thread=5, text="/qui salvati"))
        destinations.topic_command(msg(GRUPPO, thread=9, text="/qui avvisi"))
        self.assertEqual(destinations.where("saved", msg(GRUPPO, thread=3, text="x")), (-100123, 5, None))
        self.assertEqual(destinations.where("alerts"), (-100123, 9, None))
        self.assertEqual(destinations.where("saved", msg(PRIVATA, text="x")), (42, None, 7))


class TestCreator(Istanza):
    def test_archivio_custom_relazione_e_nota(self):
        simile = ideas.archive()[0]
        a = {"score": 4, "reason": "Buona.", "similar": simile}
        r = self.process(msg(text="il ponte del diavolo"), classified(category="idea_video", title="Ponte"), analysis=a)
        props, blocks = self.calls.create.call_args[0][:2]
        self.assertEqual(props["Punteggio idea"]["number"], 4)
        self.assertTrue(props["Già trattato"]["checkbox"])
        self.assertNotIn("Contenuto simile", props)  # nessuna pagina di script da collegare
        self.assertIn("già trattato (TV-001)", r["reply"])
        testo = " ".join(x["text"]["content"] for b in blocks for x in b.get(b["type"], {}).get("rich_text", []))
        self.assertIn("Già trattato: TV-001 · Il ponte del diavolo", testo)
        self.assertIn("Nessuno script per questo contenuto.", testo)

    def test_relazione_alla_pagina_di_script(self):
        a = {"score": 2, "reason": "Già fatto.", "similar": ideas.archive()[1]}
        self.process(msg(text="casa"), classified(category="idea_video", title="Casa"), analysis=a)
        props = self.calls.create.call_args[0][0]
        self.assertEqual(props["Contenuto simile"], {"relation": [{"id": "pagina-script-002"}]})

    def test_analisi_usa_id_del_archivio(self):
        with mock.patch("notion.inbox_ideas", return_value=[]), \
                mock.patch("llm.complete_json", return_value={"duplicate": "TV-002", "score": 3, "reason": "r"}) as cj:
            ideas._inbox = None
            out = ideas.analyze({"context": "x"}, {"title": "casa delle bambole", "topic": "", "summary": ""})
        self.assertIn("TV-002", cj.call_args[0][1]["properties"]["duplicate"]["enum"])
        self.assertIn("9000 views", cj.call_args[0][0])
        self.assertEqual(out["similar"]["id"], "TV-002")

    def test_bozza_con_ganci_custom(self):
        with mock.patch("llm.complete_json", return_value={"draft": "Una bozza — con trattino", "to_verify": []}) as cj:
            d = drafts.draft({"context": "x"}, {"title": "t", "topic": "a"})
        self.assertEqual(d["draft"], "Una bozza, con trattino")
        self.assertEqual(d["checks"], ["non apre con \"Sapevi che...\""])
        self.assertIn("Regole di stile di prova.", cj.call_args[0][0])
        self.assertIn("La casa delle bambole (3.0x la mediana)", cj.call_args[0][0])

    def test_bozze_su_richiesta_titoli_italiani(self):
        riga = {"id": "p1", "url": "https://notion.so/p1", "properties": {
            "Titolo": {"type": "title", "title": [{"plain_text": "Idea"}]},
            "Argomento": {"type": "rich_text", "rich_text": []}}}
        with mock.patch("notion.pages_to_draft", return_value=[riga]), \
                mock.patch("notion.page_sections", return_value={"Bozza script": "c'è"}), \
                mock.patch("notion.update_inbox_page") as up:
            self.assertEqual(pipeline.drafts_on_request(), [])
        up.assert_called_once_with("p1", {"Bozza scritta": {"checkbox": True}})


class TestCancellazione(Istanza):
    def handle(self, m, result=None, process=True):
        with mock.patch("notion.find_by_message_id", return_value=None), \
                mock.patch("pipeline.process", return_value=result or {"reply": "ok", "deletable": True}) as pr, \
                mock.patch("telegram.reply") as reply, mock.patch("telegram.react") as react, \
                mock.patch("telegram.delete") as delete, mock.patch("agent.answer", return_value="risposta"):
            main.handle(m)
        return SimpleNamespace(process=pr, reply=reply, react=react, delete=delete)

    def test_cancella_dopo_il_salvataggio(self):
        c = self.handle(msg(text="https://example.com"))
        c.delete.assert_called_once_with(42, [7])

    def test_non_cancella_se_non_salvabile(self):
        c = self.handle(msg(text="x"), {"reply": "⚠️", "deletable": False})
        c.delete.assert_not_called()

    def test_domande_e_comandi_restano(self):
        c = self.handle(msg(text="? quante idee ho"))
        c.delete.assert_not_called()
        c.process.assert_not_called()
        c = self.handle(msg(GRUPPO, thread=5, text="/qui salvati"))
        c.delete.assert_not_called()

    def test_album_cancellato_per_intero(self):
        parti = [msg(mid=i, media_group_id="g", photo=[{"file_id": f"f{i}"}]) for i in (10, 11, 12)]
        unito, _ = pipeline.merge_split([{"update_id": i, "message": p} for i, p in enumerate(parti)])[0]
        self.assertEqual(pipeline.message_ids(unito), [10, 11, 12])
        c = self.handle(unito)
        c.delete.assert_called_once_with(42, [10, 11, 12])

    def test_gruppo_senza_permessi_un_avviso_al_giorno(self):
        with mock.patch("telegram.delete", side_effect=Exception("not enough rights")), \
                mock.patch("main._alert") as alert:
            main._delete(msg(GRUPPO, thread=3, text="x"))
            main._delete(msg(GRUPPO, thread=3, text="y"))
        alert.assert_called_once_with(i18n.t("delete_not_allowed"))

    def test_in_gruppo_niente_reazione_se_cancella(self):
        destinations.topic_command(msg(GRUPPO, thread=5, text="/qui salvati"))
        c = self.handle(msg(GRUPPO, thread=3, text="x"))
        c.react.assert_not_called()
        c.delete.assert_called_once()


class TestAllegati(Istanza):
    def test_foto_caricata_su_notion(self):
        m = msg(photo=[{"file_id": "piccola"}, {"file_id": "grande", "file_size": 1000}], caption="una foto")
        with mock.patch("telegram.download", return_value=b"jpg") as dl, \
                mock.patch("notion.upload_file", return_value="up1") as up, \
                mock.patch("notion.append_files") as app:
            r = self.process(m)
        dl.assert_called_once_with("grande")
        self.assertEqual(up.call_args[0][2], "image/jpeg")
        blocchi = app.call_args[0][1]
        self.assertEqual(blocchi[0]["heading_2"]["rich_text"][0]["text"]["content"], "Allegati")
        self.assertEqual(blocchi[1]["type"], "image")
        self.assertTrue(r["deletable"])

    def test_file_troppo_grande_resta_in_chat(self):
        m = msg(document={"file_id": "f", "file_name": "film.mp4", "file_size": 30 * 1024 * 1024})
        with mock.patch("notion.upload_file") as up, mock.patch("notion.append_files"):
            r = self.process(m)
        up.assert_not_called()
        self.assertFalse(r["deletable"])
        self.assertIn("film.mp4 non salvato in Notion", r["reply"])

    def test_upload_fallito_resta_in_chat(self):
        m = msg(voice={"file_id": "v", "mime_type": "audio/ogg"})
        with mock.patch("telegram.download", return_value=b"ogg"), \
                mock.patch("media.transcribe", return_value="testo del vocale"), \
                mock.patch("notion.upload_file", side_effect=Exception("Notion 400")), \
                mock.patch("notion.append_files"):
            r = self.process(m)
        self.assertFalse(r["deletable"])

    def test_link_doppione_con_foto_non_si_cancella(self):
        with mock.patch("notion.find_by_url", return_value="https://notion.so/vecchia"):
            r = pipeline.process(msg(photo=[{"file_id": "f"}], caption="https://example.com/a"))
            r2 = pipeline.process(msg(text="https://example.com/a"))
        self.assertFalse(r["deletable"])
        self.assertTrue(r2["deletable"])
        self.assertIn("già in INBOX", r2["reply"])


class TestLibri(Istanza):
    def test_scheda_open_library(self):
        trovato = {"url": "https://openlibrary.org/works/OL1W", "title": "Il nome della rosa", "author": "Umberto Eco",
                   "year": 1980, "pages": 503, "isbn": "9788845278655", "publisher": "Bompiani",
                   "cover": "https://covers.openlibrary.org/b/id/1-M.jpg"}
        r = self.process(msg(text="da leggere: il nome della rosa"),
                         classified(category="libro", title="Romanzo", book_title="Il nome della rosa"), lookup=trovato)
        props, blocks = self.calls.create.call_args[0][:2]
        self.assertEqual(props["Titolo"]["title"][0]["text"]["content"], "Il nome della rosa")
        self.assertEqual(props["Autore"]["rich_text"][0]["text"]["content"], "Umberto Eco")  # da Open Library
        self.assertEqual(self.calls.create.call_args[1]["cover"], trovato["cover"])
        testo = " ".join(x["text"]["content"] for b in blocks for x in b.get(b["type"], {}).get("rich_text", []))
        self.assertIn("Scheda libro", testo)
        self.assertIn("Pagine: 503", testo)
        self.assertIn("Umberto Eco", r["reply"])

    def test_ricerca_e_corrispondenza(self):
        docs = {"docs": [{"key": "/works/OL2W", "title": "Altro libro"},
                         {"key": "/works/OL1W", "title": "Il nome della rosa", "author_name": ["Umberto Eco"],
                          "isbn": ["8845278654", "9788845278655"], "cover_i": 7}]}
        with mock.patch("net.request") as req:
            req.return_value.json.return_value = docs
            b = books.lookup("Il nome della rosa", "Eco")
        self.assertEqual(b["url"], "https://openlibrary.org/works/OL1W")
        self.assertEqual(b["isbn"], "9788845278655")
        self.assertEqual(b["cover"], "https://covers.openlibrary.org/b/id/7-M.jpg")
        with mock.patch("net.request") as req:
            req.return_value.json.return_value = {"docs": [{"key": "/works/X", "title": "Tutt'altro"}]}
            self.assertIsNone(books.lookup("Il nome della rosa"))


class TestSpese(Istanza):
    def uso(self, i=0, o=0, cr=0, cw=0, ricerche=0):
        return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
                               cache_creation_input_tokens=cw, server_tool_use=SimpleNamespace(web_search_requests=ricerche))

    def test_prezzi(self):
        self.assertAlmostEqual(costs.usd("claude-sonnet-5-5", self.uso(i=1_000_000, o=100_000)), 3.0)
        self.assertAlmostEqual(costs.usd("claude-opus-5-5-20261001", self.uso(cr=1_000_000, cw=1_000_000)), 5.2)
        self.assertAlmostEqual(costs.usd("claude-haiku-5-5", self.uso(ricerche=3)), 0.03)
        self.assertEqual(costs.usd("modello-sconosciuto", self.uso(i=10)), 0.0)

    def test_limite_e_avvisi(self):
        costs.record("claude-sonnet-5-5", self.uso(i=4_000_000))  # 8 USD = 7,27 €
        self.assertFalse(costs.over_limit())
        self.assertIsNone(costs.pending_alert())
        costs.record("claude-sonnet-5-5", self.uso(i=1_000_000))  # 10 USD = 9,09 €: oltre l'80%
        self.assertIn("80%", costs.pending_alert())
        self.assertIsNone(costs.pending_alert())  # una volta sola
        costs.record("claude-sonnet-5-5", self.uso(i=1_000_000))  # 12 USD = 10,91 €
        self.assertTrue(costs.over_limit())
        self.assertIn("Raggiunto il limite", costs.pending_alert())
        with self.assertRaises(costs.LimitReached):
            costs.check()
        self.assertIn("(limite €10.00)", costs.digest_line())

    def test_limite_raggiunto_salva_senza_analisi(self):
        r = self.process(msg(text="una leggenda da approfondire"), over_limit=True)
        self.calls.classify.assert_not_called()
        self.assertFalse(r["deletable"])  # non analizzato: resta in chat

    def test_agente_in_pausa_oltre_il_limite(self):
        with mock.patch("costs.over_limit", return_value=True), mock.patch("agent._runner") as run, \
                mock.patch.object(agent, "USAGE", self.root / "cache" / "uso.json"):
            self.assertIn("limite di spesa", agent.answer(msg(text="/chiedi quante idee ho")))
        run.assert_not_called()


class TestRiepilogoECopia(Istanza):
    def pagina(self, pid, titolo, categoria, minuti=None, stato="Da vedere", punteggio=None):
        p = {"Titolo": {"type": "title", "title": [{"plain_text": titolo}]},
             "Categoria": {"type": "select", "select": {"name": categoria}},
             "Stato": {"type": "select", "select": {"name": stato}},
             "Data salvataggio": {"type": "date", "date": {"start": f"2026-10-{10 + len(pid):02d}"}},
             "Tempo di lettura (min)": {"type": "number", "number": minuti},
             "Punteggio idea": {"type": "number", "number": punteggio}}
        return {"id": pid, "url": f"https://notion.so/{pid}", "properties": p}

    def test_riepilogo_con_letture_e_spesa(self):
        pagine = [self.pagina("a", "Breve", "articolo", 4), self.pagina("bb", "Lungo", "articolo", 35),
                  self.pagina("ccc", "Libro A", "libro"), self.pagina("dddd", "Idea forte", "idea_video", punteggio=5),
                  self.pagina("eeeee", "Già letto", "articolo", 3, stato="Usata")]
        costs.record("claude-haiku-5-5", SimpleNamespace(input_tokens=1_000_000, output_tokens=0))
        with mock.patch("notion.mirror", return_value=pagine):
            testo = main.digest_text(config.DIGEST)
        self.assertIn("📬 Idee migliori ancora da vedere (1):", testo)
        self.assertIn("5/5 · Idea forte", testo)
        self.assertIn("Letture brevi (fino a 10 min):\n4 min · Breve", testo)
        self.assertIn("35 min · Lungo", testo)
        self.assertNotIn("Già letto", testo)
        self.assertIn("Libri da leggere: 1", testo)
        self.assertIn("💶 Spesa:", testo)

    def test_copia_locale_incrementale(self):
        riga = lambda pid: {"id": pid, "url": f"u/{pid}", "properties": {}}  # noqa: E731
        with mock.patch("notion.query", return_value=[riga("a"), riga("b")]) as q:
            self.assertEqual(len(notion.mirror()), 2)
        self.assertEqual(q.call_args[0][1:], ())  # prima volta: lettura completa
        with mock.patch("notion.query", return_value=[riga("c")]) as q:
            self.assertEqual({p["id"] for p in notion.mirror()}, {"a", "b", "c"})
        self.assertEqual(q.call_args[0][1]["filter"]["timestamp"], "last_edited_time")

    def test_agente_cerca_nella_copia_locale(self):
        pagine = [self.pagina("a", "Il ponte del diavolo", "idea_video", punteggio=4),
                  self.pagina("b", "Torta di mele", "ricetta")]
        with mock.patch("notion.mirror", return_value=pagine):
            out = json.loads(agent.search_inbox.call({"text": "ponte", "status": "Da vedere"}))
        self.assertEqual([p["title"] for p in out["pages"]], ["Il ponte del diavolo"])
        self.assertIn("idea_bot", agent.system_prompt())
        self.assertTrue(agent.is_question(msg(text="/chiedi quante idee")))
        self.assertFalse(agent.is_question(msg(text="/ask quante idee")))

    def test_doppioni_distinti_per_chat(self):
        riga = lambda chat: {"url": "vecchia", "properties": {"Telegram chat ID": {"number": chat}}}  # noqa: E731
        with mock.patch("notion.query", return_value=[riga(None)]):
            self.assertEqual(notion.find_by_message_id(12, 42, private=True), "vecchia")
            self.assertIsNone(notion.find_by_message_id(12, -100123, private=False))
        with mock.patch("notion.query", return_value=[riga(42)]):
            self.assertIsNone(notion.find_by_message_id(12, -100123, private=False))



class TestParitaConVersionePrecedente(Istanza):
    """Comportamenti della prima versione del bot che un'istanza personale deve conservare."""

    def test_video_social_non_classificato_va_in_idee(self):
        with mock.patch("notion.find_by_url", return_value=None), \
                mock.patch("media.video", side_effect=Exception("bloccato")), \
                mock.patch("pipeline.web_preview", return_value=""), \
                mock.patch("pipeline.web_page", return_value={"preview": "", "words": 0}), \
                mock.patch("pipeline.classify", side_effect=Exception("API giù")), \
                mock.patch("books.lookup", return_value=None), mock.patch("costs.over_limit", return_value=False), \
                mock.patch("notion.create_inbox_page", return_value=PAGE_URL) as create:
            r = pipeline.process(msg(text="https://www.tiktok.com/@x/video/1"))
            r2 = pipeline.process(msg(text="https://example.com/pagina"))
        self.assertEqual(create.call_args_list[0][0][0]["Categoria"]["select"]["name"], "idea_video")
        self.assertEqual(create.call_args_list[1][0][0]["Categoria"]["select"]["name"], "altro")
        errore = create.call_args_list[0][0][0]["Errore"]["rich_text"][0]["text"]["content"]
        self.assertIn("Video: bloccato", errore)
        self.assertIn("Classificazione: API giù", errore)
        self.assertFalse(r["deletable"])

    def test_cerca_pubblicati_con_archivio_da_custom(self):
        nomi = [getattr(t, "name", None) or t["name"] for t in agent.tools()]
        self.assertIn("search_published", nomi)
        self.assertIn("write_draft", nomi)
        self.assertIn("on what they published", agent.system_prompt() + " on what they published")
        out = json.loads(agent.search_published.call({"text": "ponte del diavolo"}))
        self.assertEqual(out[0]["id"], "TV-001")
        self.assertEqual(out[0]["views"], 1200)

    def test_testi_al_modello_in_italiano(self):
        r = SimpleNamespace(url="https://x.it/a", encoding="utf-8",
                            raw=SimpleNamespace(read=lambda *a, **k: b"<title>Il titolo</title><meta name='description' content='Desc'>"))
        with mock.patch("net.request", return_value=r):
            prev = pipeline.web_page("https://x.it/a")["preview"]
        self.assertEqual(prev, "Pagina: https://x.it/a\nTitolo pagina: Il titolo\nDescrizione pagina: Desc")
        with mock.patch("llm.complete_json", return_value={"duplicate": "", "score": 2, "reason": "r"}) as cj, \
                mock.patch("notion.inbox_ideas", return_value=[]):
            ideas._inbox = None
            ideas.analyze({}, {"title": "argomento senza riscontri", "topic": "", "summary": ""})
        self.assertIn("(solo il riassunto)", cj.call_args[0][0])
        self.assertIn("(nessuno)", cj.call_args[0][0])


if __name__ == "__main__":
    unittest.main()

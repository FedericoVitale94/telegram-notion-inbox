"""custom.py di prova: archivio da due database simulati, esempi e controlli delle bozze."""


def archive():
    return [{"id": "TV-001", "title": "Il ponte del diavolo", "text": "leggenda del ponte del diavolo",
             "script": "", "score": None, "url": "https://example.com/tv-001", "date": "2026-01-10", "views": 1200,
             "relation_id": None, "similar_note": "Nessuno script per questo contenuto."},
            {"id": "TV-002", "title": "La casa delle bambole", "text": "casa infestata", "script": "testo " * 60,
             "score": 5, "url": "https://example.com/tv-002", "date": "2026-02-01", "views": 9000,
             "relation_id": "pagina-script-002"}]


def draft_examples():
    return [{"title": "La casa delle bambole", "script": "testo dello script", "note": "3.0x la mediana"}]


def draft_postprocess(text):
    return text.replace(" — ", ", ")


def draft_checks(text, lo, hi):
    return [] if text.startswith("Sapevi che") else ["non apre con \"Sapevi che...\""]

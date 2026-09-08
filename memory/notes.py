"""Opt-in notes, not a vector database. Do not auto-save raw documents."""
import re
import sqlite3
from pathlib import Path


def redact(text):
    text = re.sub(r'https?://\S+', '[URL]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?i)(?:api[_-]?key|password|token|secret)\s*[:=]\s*\S+', '[SECRET]', text)
    text = re.sub(r'\b(?:sk-|ghp_)[A-Za-z0-9_-]+', '[SECRET]', text)
    text = re.sub(r'(?<!\w)\+?[\d ()-]{8,}\d(?!\w)', '[NUMBER]', text)
    return text[:1000]


def terms(text):
    # English words + Chinese adjacent character pairs, no embedding service.
    words = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for span in re.findall(r'[\u4e00-\u9fff]+', text):
        words.update(span[i:i + 2] for i in range(max(1, len(span) - 1)))
    return words


class NoteMemory:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS memory_notes (task_id TEXT PRIMARY KEY, note TEXT NOT NULL)')

    def remember(self, task_id, note):
        clean = redact(note)
        if clean.strip():
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT OR REPLACE INTO memory_notes VALUES (?, ?)', (task_id, clean))

    def search(self, query, limit=3):
        query_terms = terms(query)
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT task_id, note FROM memory_notes ORDER BY rowid DESC LIMIT 1000').fetchall()
        scored = [(len(query_terms & terms(note)), task_id, note) for task_id, note in rows]
        return [{'task_id': task_id, 'note': note} for score, task_id, note
                in sorted(scored, reverse=True)[:limit] if score > 0]

    def forget(self, task_id):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM memory_notes WHERE task_id=?', (task_id,))

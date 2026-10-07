"""SQLite storage for the library.

The library folder holds the PDFs and `gloss.db`.  Summaries, briefs and page
images stay in ~/.cache/gloss keyed by content hash, exactly as the reader
expects; the database is an index over them, not a replacement.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path



def _default_library() -> Path:
    """GLOSS_LIBRARY from the environment or the project's .env, else ~/Gloss Library."""
    value = os.environ.get("GLOSS_LIBRARY", "").strip()
    if not value:
        env = Path(__file__).resolve().parent.parent / ".env"
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                name, _, rest = line.partition("=")
                if name.strip() == "GLOSS_LIBRARY":
                    value = rest.strip().strip("\"'")
        except OSError:
            pass
    return Path(value).expanduser() if value else Path.home() / "Gloss Library"


DEFAULT_LIBRARY = _default_library()

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  sha TEXT PRIMARY KEY,            -- content hash (summarize.sha_of)
  pdf_path TEXT NOT NULL,
  filename TEXT,
  title TEXT, year INTEGER, venue TEXT, doi TEXT,
  abstract TEXT,
  keywords_json TEXT,              -- ["concept", ...]
  ref_text TEXT,                   -- normalized reference-list text, for citation matching
  norm_title TEXT,
  duplicate_of TEXT,               -- sha of the paper this repeats, else NULL
  meta_status TEXT DEFAULT 'pending',   -- pending | ok | failed
  added_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS authors (
  id INTEGER PRIMARY KEY,
  name_norm TEXT UNIQUE,
  display_name TEXT
);
CREATE TABLE IF NOT EXISTS paper_authors (
  sha TEXT, author_id INTEGER, ord INTEGER,
  PRIMARY KEY (sha, author_id)
);
CREATE TABLE IF NOT EXISTS edges (
  a TEXT, b TEXT,                  -- sha pair, a < b
  etype TEXT,                      -- shared_author | citation | shared_keyword | llm_related
  weight REAL,
  detail TEXT,                     -- JSON
  PRIMARY KEY (a, b, etype)
);
CREATE TABLE IF NOT EXISTS judged (   -- pairs the LLM has already ruled on
  a TEXT, b TEXT, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS qa (
  id INTEGER PRIMARY KEY,
  sha TEXT, question TEXT, answer TEXT, cited_json TEXT,
  asked_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pa_author ON paper_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_qa_sha ON qa(sha);
"""

_local = threading.local()
_path: Path | None = None


def library_dir() -> Path:
    return _path or DEFAULT_LIBRARY


def init(folder: str | os.PathLike | None = None) -> Path:
    """Point the library at a folder (creating it and the database)."""
    global _path
    _path = Path(folder) if folder else DEFAULT_LIBRARY
    _path.mkdir(parents=True, exist_ok=True)
    _local.__dict__.clear()
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        for col, ddl in (("brief", "TEXT"), ("summary_status", "TEXT DEFAULT 'none'")):
            if col not in have:                       # databases made by earlier versions
                conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {ddl}")
    return _path


def connect() -> sqlite3.Connection:
    """One connection per thread; Flask serves requests on several."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(library_dir() / "gloss.db", timeout=30)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn

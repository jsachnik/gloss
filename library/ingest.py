"""Add PDFs to the library: hash, read metadata, spot duplicates, link up."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import summarize

from . import db, edges
from .metadata import extract_metadata, norm_author, norm_text

WORKERS = 4


def list_pdfs(folder: Path | None = None) -> list[Path]:
    folder = folder or db.library_dir()
    return sorted(p for p in folder.rglob("*.pdf") if p.is_file())


def _store(conn, sha: str, path: Path, meta: dict) -> str | None:
    """Insert one paper; return the sha it duplicates, if any."""
    nt = norm_text(meta["title"])
    dup = None
    if meta.get("doi"):
        row = conn.execute("SELECT sha FROM papers WHERE doi=? AND duplicate_of IS NULL",
                           (meta["doi"],)).fetchone()
        dup = row["sha"] if row else None
    if not dup and len(nt) >= 20:
        row = conn.execute("SELECT sha FROM papers WHERE norm_title=? AND duplicate_of IS NULL",
                           (nt,)).fetchone()
        dup = row["sha"] if row else None

    conn.execute(
        """INSERT OR REPLACE INTO papers(sha,pdf_path,filename,title,year,venue,doi,abstract,
               keywords_json,ref_text,norm_title,duplicate_of,meta_status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ok')""",
        (sha, str(path), path.name, meta["title"], meta["year"], meta["venue"], meta["doi"],
         meta["abstract"], json.dumps(meta["keywords"]), meta["ref_text"], nt, dup))
    if not dup:
        for order, name in enumerate(meta["authors"]):
            norm = norm_author(name)
            if not norm:
                continue
            conn.execute("INSERT OR IGNORE INTO authors(name_norm, display_name) VALUES(?,?)",
                         (norm, name))
            aid = conn.execute("SELECT id FROM authors WHERE name_norm=?", (norm,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO paper_authors(sha,author_id,ord) VALUES(?,?,?)",
                         (sha, aid, order))
    conn.commit()
    return dup


def scan(folder: Path | None = None, use_llm_edges: bool = True, log=print) -> dict:
    """Ingest every PDF in the library folder that is not already known."""
    conn = db.connect()
    known_paths = {r["pdf_path"] for r in conn.execute("SELECT pdf_path FROM papers")}
    report = {"added": [], "duplicates": [], "same_file": [], "errors": []}

    fresh = []
    for path in list_pdfs(folder):
        if str(path) in known_paths:
            continue
        sha = summarize.sha_of(str(path))
        if conn.execute("SELECT 1 FROM papers WHERE sha=?", (sha,)).fetchone():
            report["same_file"].append(path.name)         # byte-identical copy
            continue
        fresh.append((sha, path))

    def read(item):
        sha, path = item
        try:
            return sha, path, extract_metadata(str(path), path.name), None
        except Exception as exc:                          # noqa: BLE001
            return sha, path, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(read, fresh))

    for sha, path, meta, err in results:                  # sequential: duplicate checks need order
        if err:
            report["errors"].append((path.name, err))
            log(f"  FAILED {path.name}: {err}")
            continue
        dup = _store(conn, sha, path, meta)
        if dup:
            report["duplicates"].append((path.name, dup))
            log(f"  duplicate: {path.name}")
        else:
            report["added"].append(sha)
            log(f"  added: {meta['title'][:70]}")

    for sha in report["added"]:
        edges.rebuild_for(sha, use_llm=use_llm_edges)
        log(f"  linked {sha}")
    return report

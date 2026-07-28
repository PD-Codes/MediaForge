"""Reading a Calibre library's ``metadata.db``, strictly read-only.

Calibre keeps its authoritative catalogue in an SQLite database at the root of
the library. It holds what the individual ``metadata.opf`` files often lack --
series and volume number, tags, rating, the long description -- so it is worth
consulting when it happens to be there.

Two rules govern this module, and both are about not breaking the user's
Calibre install:

* the database is opened through a ``file:...?mode=ro&immutable=1`` URI, so
  SQLite neither writes to it nor creates the ``-wal``/``-shm`` sidecars that a
  normal open would, and it never takes a lock Calibre would then trip over;
* every failure is soft. A missing file, a running Calibre, a schema from a
  version that renamed a column -- all of it degrades to "no data from here",
  because a library scan that dies on an optional metadata source is worse than
  one that simply has less to show.
"""
from __future__ import annotations

import sqlite3

from ...logger import get_logger
from .identity import normalize

logger = get_logger(__name__)

# Guard against a pathological library: this is a lookup table held in memory
# for the duration of one scan, not a mirror of the whole catalogue.
_MAX_ROWS = 200_000

_QUERY = """
SELECT b.id,
       b.title,
       b.sort,
       b.series_index,
       b.pubdate,
       (SELECT group_concat(a.name, ' & ')
          FROM books_authors_link bal JOIN authors a ON a.id = bal.author
         WHERE bal.book = b.id)                                   AS authors,
       (SELECT s.name
          FROM books_series_link bsl JOIN series s ON s.id = bsl.series
         WHERE bsl.book = b.id)                                   AS series,
       (SELECT group_concat(t.name, ',')
          FROM books_tags_link btl JOIN tags t ON t.id = btl.tag
         WHERE btl.book = b.id)                                   AS tags,
       (SELECT c.text FROM comments c WHERE c.book = b.id)         AS description,
       (SELECT i.val FROM identifiers i
         WHERE i.book = b.id AND i.type = 'isbn')                  AS isbn
  FROM books b
"""


def load_catalogue(db_path) -> dict:
    """Return ``{normalised-title: [record, ...]}`` for one Calibre database.

    Keyed by normalised title rather than by path on purpose: the database
    knows nothing about where MediaForge found a file, and matching on the
    title is exactly the join the rest of the book pipeline already performs.
    """
    try:
        if not db_path.is_file():
            return {}
    except OSError:
        return {}

    uri = "file:{}?mode=ro&immutable=1".format(str(db_path).replace("?", "%3f"))
    catalogue: dict = {}
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_QUERY).fetchmany(_MAX_ROWS)
    except Exception:
        # Includes the schema-drift case: an older or newer Calibre that does
        # not have one of these tables raises OperationalError here.
        logger.info("[Books] Calibre metadata.db not usable: %s", db_path)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for row in rows:
        title = (row["title"] or "").strip()
        if not title:
            continue
        record = {"title": title}
        authors = (row["authors"] or "").strip()
        if authors:
            record["authors"] = [a.strip() for a in authors.split("&") if a.strip()]
        if row["series"]:
            record["series"] = row["series"].strip()
            try:
                record["series_index"] = float(row["series_index"] or 0) or None
            except (TypeError, ValueError):
                record["series_index"] = None
        if row["tags"]:
            record["tags"] = [t.strip() for t in row["tags"].split(",") if t.strip()][:20]
        if row["description"]:
            record["description"] = row["description"]
        if row["isbn"]:
            record["isbn"] = str(row["isbn"]).strip()
        if row["pubdate"]:
            record["published"] = str(row["pubdate"])[:10]
        catalogue.setdefault(normalize(title), []).append(record)

    return catalogue


def lookup(catalogue: dict, title: str, author: str) -> dict:
    """Best record for a title, disambiguated by author when needed."""
    records = catalogue.get(normalize(title)) or []
    if not records:
        return {}
    if len(records) == 1:
        return records[0]
    norm_author = normalize(author)
    if norm_author:
        for record in records:
            for candidate in record.get("authors") or []:
                if normalize(candidate) == norm_author:
                    return record
    # Several records, no author to tell them apart: prefer the one that
    # carries the most information rather than picking arbitrarily.
    return max(records, key=lambda r: len(r))

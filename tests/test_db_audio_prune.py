"""prune_audio_track_cache() must walk past rows of files that still exist (P23-08).

It used to select the same oldest 500 rows every run; once 500 old rows
belonged to files that are still there, rows of deleted files behind them were
never reached.
"""


def test_prune_reaches_stale_rows_behind_existing_ones(app, tmp_path):
    from mediaforge.web import db
    from mediaforge.web.db import audio_tracks

    db.init_audio_track_db()
    present = []
    for i in range(600):
        f = tmp_path / ("present-%03d.mkv" % i)
        f.write_bytes(b"x")
        present.append(str(f))
    conn = db.get_db()
    try:
        # 600 old rows whose file exists, then 100 newer (still old) stale rows.
        rows = [(path, 1000.0 + i) for i, path in enumerate(present)]
        rows += [(str(tmp_path / ("gone-%03d.mkv" % i)), 5000.0 + i) for i in range(100)]
        conn.executemany(
            "INSERT OR REPLACE INTO audio_track_cache (path, mtime, size, probed_at) "
            "VALUES (?, 0, 0, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    audio_tracks._PRUNE_CURSOR[0] = None
    removed = [db.prune_audio_track_cache() for _ in range(3)]
    conn = db.get_db()
    try:
        left_stale = conn.execute(
            "SELECT COUNT(*) FROM audio_track_cache WHERE path LIKE ?",
            (str(tmp_path / "gone-") + "%",)).fetchone()[0]
        conn.execute("DELETE FROM audio_track_cache WHERE path LIKE ?", (str(tmp_path) + "%",))
        conn.commit()
    finally:
        conn.close()
    assert sum(removed) == 100, removed
    assert left_stale == 0

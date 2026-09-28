from scripts.rag import connect, retrieve, update


def test_local_rag_cites_bounds_and_removes_stale_content(tmp_path):
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "design.md"
    doc.write_text("# Recorder\nPIT universe uses prior-day volume.\n", encoding="utf-8")
    db = tmp_path / "run" / "rag" / "index.sqlite"
    with connect(db) as conn:
        assert update(conn, tmp_path) == (1, 1)
        found = retrieve(conn, "PIT universe", "docs", 4, 300)
        assert "[docs/design.md:1-2]" in found and "prior-day volume" in found
        assert len(found) <= 300
        assert update(conn, tmp_path) == (0, 1)
        doc.write_text("# Recorder\nCurrent funding is settled first.\n", encoding="utf-8")
        assert update(conn, tmp_path) == (1, 1)
        assert retrieve(conn, "prior-day", "docs", 4, 300) == "No matches."
        doc.unlink()
        assert update(conn, tmp_path) == (1, 0)
        assert retrieve(conn, "funding", "docs", 4, 300) == "No matches."

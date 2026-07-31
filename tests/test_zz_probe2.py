import time


def test_probe(app, as_user, tmp_path):
    from mediaforge.web import db
    with app.app_context():
        cp_id = db.add_custom_path("eBooks", "/tmp/calibretest/X_Calibre", media_kinds="book")
    c = as_user("admin")
    r = c.get("/api/library?kind=book")
    d = r.get_json()
    print("PASS1 is_scanning:", d.get("is_scanning"),
          "locations:", [(loc.get("label"), len(loc.get("books") or [])) for loc in d.get("locations") or []])
    for _ in range(60):
        time.sleep(0.5)
        d = c.get("/api/library?kind=book").get_json()
        if not d.get("is_scanning"):
            break
    print("PASS2 is_scanning:", d.get("is_scanning"))
    for loc in d.get("locations") or []:
        print("   loc", loc.get("label"), "books:", len(loc.get("books") or []),
              "version:", loc.get("books_version"))
        for b in (loc.get("books") or [])[:5]:
            print("      *", b.get("title"), b.get("authors"))
    with app.app_context():
        db.remove_custom_path(cp_id)

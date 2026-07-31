
import pytest
def test_probe(as_user):
    r = as_user("user").get("/settings")
    print("NON-ADMIN /settings ->", r.status_code, r.headers.get("Location"))
    body = r.get_data(as_text=True) if r.status_code == 200 else ""
    print("has spLayout-user:", "spLayout-user" in body)

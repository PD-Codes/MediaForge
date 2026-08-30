"""The MediaForge server (store / telemetry / devInfo / domain feed) must have
the old softarchiv.com domain as a working fallback -- and must NOT show up in
the content-source UIs that DEFAULT_SITE_MIRRORS drives.
"""

from mediaforge import mirrors
from mediaforge.domain_resolver import DEFAULT_DOMAINS_URL
from mediaforge.telemetry.registry import TELEMETRY_INGEST_URL
from mediaforge.web.thirdparties.store import DEFAULT_STORE_URL, _fallback_urls

NEW = "mediaforge.pd-codes.net"
OLD = "mediaforge.softarchiv.com"


def test_all_first_party_urls_use_the_new_host():
    for url in (DEFAULT_STORE_URL, TELEMETRY_INGEST_URL, DEFAULT_DOMAINS_URL):
        assert NEW in url, url
        assert OLD not in url, url


def test_old_host_is_the_fallback():
    hosts = mirrors.get_mirrors("mediaforge")
    assert hosts[0] == NEW
    assert OLD in hosts
    assert mirrors.site_for_host(OLD) == "mediaforge"
    assert mirrors.site_for_host(NEW) == "mediaforge"


def test_store_client_walks_both_domains():
    urls = _fallback_urls(f"https://{NEW}/store/index.json")
    assert urls == [f"https://{NEW}/store/index.json", f"https://{OLD}/store/index.json"]
    # A third-party repo keeps exactly one candidate: its own URL.
    assert _fallback_urls("https://example.org/store/index.json") == [
        "https://example.org/store/index.json"]


def test_infra_host_stays_out_of_the_content_source_uis():
    # DEFAULT_SITE_MIRRORS / SITE_LABELS feed the mirror editor, the custom-path
    # "default for site" dropdown and the source multiselects.
    assert "mediaforge" not in mirrors.DEFAULT_SITE_MIRRORS
    assert "mediaforge" not in mirrors.SITE_LABELS


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")

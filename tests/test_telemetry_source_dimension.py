"""Which SITE a download came from -- the dimension the hoster never carried.

`build_download_event(provider=...)` is the HOSTER (VOE, MegaPlay). Several
sites embed the same hosters, so it never answered whether anybody actually
uses filmo.to, 9anime or aniwaves.ru.

The interesting half of these tests is what the builders REFUSE: the value
ends up as a `feature_key` row and an indexed column on a public server, so
anything outside the closed built-in list has to produce nothing at all.
"""

import pytest

from mediaforge.telemetry import events, registry, settings


@pytest.fixture
def consented(monkeypatch):
    """Consent granted for the source dimension and the network detail key."""
    monkeypatch.setattr(settings, "is_key_enabled",
                        lambda key: key in ("flag.sources", "detail.network",
                                            "downloads.titles", "downloads.errors"))


@pytest.fixture
def no_consent(monkeypatch):
    monkeypatch.setattr(settings, "is_key_enabled", lambda key: False)


# ── the key mapping itself ──────────────────────────────────────────────────
@pytest.mark.parametrize("source_id, expected", [
    ("filmo", "flag.source.filmo"),
    ("nineanime", "flag.source.nineanime"),
    ("aniwaves", "flag.source.aniwaves"),
    ("aniworld", "flag.source.aniworld"),
    # The adult source keeps its own hard-limited key and must never gain a second.
    ("hanime", None),
    ("hanime_tv", None),
    # A module's source id is text its author chose; it must not become a
    # feature_key on a public server.
    ("my_module_source", None),
    ("../../etc/passwd", None),
    ("<script>", None),
    ("", None),
    (None, None),
])
def test_source_flag_key(source_id, expected):
    assert registry.source_flag_key(source_id) == expected


def test_consent_is_asked_once_for_the_whole_family():
    """One switch in the privacy dialog, one data_key per site on the wire --
    see the flag.sources registry entry for why the two differ."""
    assert registry.consent_key_for("flag.source.filmo") == "flag.sources"
    assert registry.consent_key_for("flag.source.aniwaves") == "flag.sources"
    # Everything else is governed by itself.
    assert registry.consent_key_for("flag.autosync") == "flag.autosync"
    assert registry.consent_key_for("downloads.titles") == "downloads.titles"


# ── the usage counter ───────────────────────────────────────────────────────
def test_a_built_in_source_produces_its_own_key(consented):
    event = events.build_source_usage_event("filmo")
    assert event and event["data_key"] == "flag.source.filmo"


@pytest.mark.parametrize("source_id", ["hanime", "hanime_tv", "my_module", "", None])
def test_nothing_is_built_for_a_source_we_may_not_name(consented, source_id):
    assert events.build_source_usage_event(source_id) is None


def test_without_consent_nothing_is_built(no_consent):
    assert events.build_source_usage_event("filmo") is None


# ── the download payload ────────────────────────────────────────────────────
def _download(**kwargs):
    return events.build_download_event(
        provider="VOE", media_type="series", title="Some Show", **kwargs)


def test_the_download_event_carries_the_site(consented):
    payloads = [e["payload"] for e in _download(source="filmo")]
    assert all(p["source"] == "filmo" for p in payloads)
    assert all(p["provider"] == "VOE" for p in payloads), "the hoster must survive too"


@pytest.mark.parametrize("source", ["hanime", "some_module", "", None, "x" * 200])
def test_an_unnameable_source_omits_the_field_rather_than_guessing(consented, source):
    for event in _download(source=source):
        assert "source" not in event["payload"]


def test_the_field_is_simply_absent_for_an_old_call_site(consented):
    """Every existing caller omits `source` entirely -- that must keep working
    and must not start sending a placeholder."""
    for event in _download():
        assert "source" not in event["payload"]


# ── network problems ────────────────────────────────────────────────────────
def test_dns_fallback_carries_no_host(consented):
    event = events.build_network_detail_event("dns_fallback")
    assert event and event["payload"]["action"] == "dns_fallback"
    # The hostname would say which site was being visited; the resolver name is
    # the user's own network configuration. Neither belongs in this event.
    assert "metadata" not in event["payload"]


def test_a_source_outage_names_the_source(consented):
    event = events.build_network_detail_event("source_unavailable", "aniwaves")
    assert event["payload"]["metadata"] == {"source": "aniwaves"}


def test_an_outage_of_something_we_cannot_name_is_still_counted(consented):
    """Anonymously: the count is useful, a module's id is not ours to send."""
    event = events.build_network_detail_event("source_unavailable", "some_module")
    assert event["payload"]["metadata"] == {"source": "other"}


def test_an_unknown_action_is_not_invented(consented):
    """`action` lands in an indexed column, so a typo at a call site must not
    be able to create a new server-side value."""
    assert events.build_network_detail_event("oops") is None


def test_network_events_need_their_own_consent(no_consent):
    assert events.build_network_detail_event("dns_fallback") is None

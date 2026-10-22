"""Mediascan: a failed fetch raises (so the cache is kept) and Jellyfin is paged."""

import io
import json
import urllib.request

import pytest

from mediaforge.web import mediascan as ms


def _resp(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_jellyfin_is_paged(monkeypatch):
    seen = []

    def fake(url, timeout=0):
        seen.append(url)
        start = int(url.split("StartIndex=")[1].split("&")[0])
        total = 1500 if "Series" in url else 0
        n = min(1000, max(0, total - start))
        return _resp({"Items": [{"Name": "x%d" % i, "ProviderIds": {}} for i in range(n)],
                      "TotalRecordCount": total})

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert len(ms._mediascan_fetch_jellyfin("http://jf", "k")) == 1500
    assert sum("Series" in u for u in seen) == 2


def test_failed_fetch_raises_instead_of_returning_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError):
        ms._mediascan_fetch_jellyfin("http://jf", "k")
    with pytest.raises(RuntimeError):
        ms._mediascan_fetch_plex("http://plex", "k")

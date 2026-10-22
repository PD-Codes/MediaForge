"""Environment/third-party failures must stay out of the crash channel (see telemetry/classify.py)."""

import socket
import sqlite3
import sys
import types

import pytest

from mediaforge.telemetry.classify import (
    DNS_HINT, is_corrupt_source_stream, is_dns_failure, is_local_storage_error,
    is_not_a_defect, is_transport_failure, is_user_cancellation,
)


def test_browser_dns_failure_is_transport_and_dns():
    msg = "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://s.to/serie/ted/staffel-1/episode-5"
    assert is_transport_failure(message=msg)
    assert is_dns_failure(msg)
    assert not is_dns_failure("Read timed out")


def test_db_locked_and_nfs_eagain_are_storage():
    assert is_local_storage_error(message="database is locked")
    assert is_local_storage_error(message="[Errno 11] Resource temporarily unavailable: '/mnt/storage/MEDIA/TV Series/Dune Prophecy'")


def test_german_upscale_cancel():
    assert is_user_cancellation(message="Upscaling abgebrochen.")
    assert not is_user_cancellation(message="Serie 'Abgebrochen' nicht gefunden")


def test_corrupt_source_and_other_noise():
    assert is_corrupt_source_stream("[in#0/mpegts] Packet corrupt (stream = 1, dts = 786449).")
    assert is_not_a_defect(message="[h264] FMO is not implemented. Update your FFmpeg version")
    assert is_not_a_defect(message="404 Client Error: Not Found for url: https://api.themoviedb.org/3/movie/76124")
    assert is_not_a_defect(message="[WinError 10038] Ein Vorgang bezog sich auf ein Objekt, das kein Socket ist")


def test_real_defects_still_reported():
    assert not is_not_a_defect(message="ffmpeg error: Unrecognized option 'foo'")
    assert not is_not_a_defect(message="404 Client Error: Not Found for url: https://example.org/api")
    assert not is_local_storage_error(message="KeyError: 'title'")


def test_sto_modal_picks_resolvable_mirror(monkeypatch):
    pytest.importorskip("niquests")
    from mediaforge.playwright import captcha

    def fake_getaddrinfo(host, port):
        if host == "s.to":
            raise socket.gaierror("nope")
        return [("x",)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert captcha._resolvable_sto_host("https://s.to/serie/ted/staffel-2/episode-8") == "serienstream.to"

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: (_ for _ in ()).throw(socket.gaierror("nope")))
    with pytest.raises(RuntimeError, match="DNS"):
        captcha._resolvable_sto_host("https://s.to/serie/ted/staffel-2/episode-8")


def test_db_locked_gets_a_hint():
    from mediaforge.web.db import _core

    class Conn:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    with pytest.raises(sqlite3.OperationalError, match="database is locked.*network share"):
        _core._configure_connection(Conn())

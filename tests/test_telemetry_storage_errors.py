"""Storage problems on the media folders must never reach the crash channel.

A read-only mount or a full volume repeats for every queue item, so one
misconfigured folder used to produce a flood of identical crash reports.
"""

import errno

from mediaforge.telemetry.classify import is_local_storage_error, is_transport_failure


def test_permission_denied_is_filtered():
    exc = PermissionError(errno.EACCES, "Permission denied")
    assert is_local_storage_error(type(exc), exc)
    assert is_local_storage_error(message="[Errno 13] Permission denied: '/media/Filme'")
    assert is_local_storage_error(message="Zugriff verweigert")


def test_disk_full_is_filtered():
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert is_local_storage_error(type(exc), exc)
    assert is_local_storage_error(message="ffmpeg: No space left on device")
    assert is_local_storage_error(message="Nicht genügend Speicherplatz auf dem Datenträger")


def test_wrapped_oserror_is_found_through_the_chain():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as inner:
            raise RuntimeError("Encoding failed") from inner
    except RuntimeError as outer:
        assert is_local_storage_error(type(outer), outer)


def test_real_defects_still_reported():
    exc = ValueError("season parsing failed for S02E13")
    assert not is_local_storage_error(type(exc), exc)
    assert not is_local_storage_error(message="Downloaded file is empty")
    # and the storage filter must not swallow network failures either way round
    assert not is_transport_failure(message="[Errno 13] Permission denied: '/media'")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")

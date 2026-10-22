"""requests adapters must have HTTP/3 disabled (see mediaforge.net_compat)."""
import pytest
import requests

import mediaforge  # noqa: F401  (applies the patch on import)


def test_requests_adapter_disables_http3():
    urllib3 = pytest.importorskip("urllib3")
    if not hasattr(urllib3, "HttpVersion"):
        pytest.skip("stock urllib3 has no HTTP/3")
    adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1)
    assert urllib3.HttpVersion.h3 in adapter.poolmanager.connection_pool_kw["disabled_svn"]

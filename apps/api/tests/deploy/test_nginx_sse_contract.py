"""Nginx SSE 反代配置合同测试。"""

import re
from pathlib import Path

NGINX_CONFIG = Path(__file__).resolve().parents[4] / "deploy/nginx/default.conf"
SSE_LOCATIONS = ("/api/events", "/api/chat/messages/stream")


def _location_block(config: str, path: str) -> str:
    match = re.search(
        rf"(?ms)^    location = {re.escape(path)} \{{\n(?P<body>.*?)(?=^    \}})",
        config,
    )
    assert match is not None, f"missing exact SSE location: {path}"
    return match.group("body")


def test_nginx_keeps_both_sse_locations_unbuffered() -> None:
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    required_directives = (
        "proxy_http_version 1.1;",
        "proxy_buffering off;",
        "proxy_cache off;",
        "proxy_read_timeout 3600s;",
    )

    for path in SSE_LOCATIONS:
        block = _location_block(config, path)
        assert f"proxy_pass http://api:8000{path};" in block
        for directive in required_directives:
            assert directive in block
        assert re.search(r"proxy_set_header Connection (?:\"\"|'');", block)


def test_nginx_keeps_regular_api_proxy_buffering_default() -> None:
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    generic = re.search(r"(?ms)^    location /api/ \{\n(?P<body>.*?)(?=^    \})", config)
    assert generic is not None
    assert "proxy_buffering off;" not in generic.group("body")
    assert "proxy_read_timeout 3600s;" not in generic.group("body")

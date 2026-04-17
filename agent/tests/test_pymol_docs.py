"""Tests for the ``lookup_pymol_docs`` / ``_fetch_pymol_wiki`` helper."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_server.remote_tool import _fetch_pymol_wiki, _WikiTextExtractor


FAKE_HTML = """\
<html><body>
<div id="mw-content-text">
<p>The <b>iterate</b> command executes a Python expression.</p>
<script>var x = 1;</script>
<p>Use <code>iterate_state</code> for coordinates.</p>
</div>
<footer>Site footer</footer>
</body></html>
"""


def test_text_extractor_extracts_content_div() -> None:
    ext = _WikiTextExtractor()
    text = ext.extract(FAKE_HTML)
    assert "iterate" in text
    assert "iterate_state" in text
    assert "var x = 1" not in text
    assert "Site footer" not in text


def test_text_extractor_returns_empty_for_missing_div() -> None:
    ext = _WikiTextExtractor()
    assert ext.extract("<html><body><p>no content div</p></body></html>") == ""


def test_fetch_truncates_long_text() -> None:
    long_html = (
        '<div id="mw-content-text"><p>'
        + "A" * 6000
        + "</p></div>"
    )
    mock_resp = MagicMock()
    mock_resp.read.return_value = long_html.encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _fetch_pymol_wiki("Iterate")

    assert "truncated" in result
    assert len(result) < 5000


def test_fetch_returns_error_on_network_failure() -> None:
    with patch("urllib.request.urlopen", side_effect=ConnectionError("offline")):
        result = _fetch_pymol_wiki("Iterate")

    assert result.startswith("[ERROR]")
    assert "offline" in result


def test_fetch_returns_error_on_404() -> None:
    import urllib.error

    exc = urllib.error.HTTPError(
        url="https://pymolwiki.org/index.php/Nonexistent",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=None,
    )
    with patch("urllib.request.urlopen", side_effect=exc):
        result = _fetch_pymol_wiki("Nonexistent")

    assert result.startswith("[ERROR]")
    assert "404" in result


def test_fetch_capitalizes_command() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = FAKE_HTML.encode()

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        _fetch_pymol_wiki("iterate")

    called_url = mock_open.call_args[0][0].full_url
    assert "/Iterate" in called_url

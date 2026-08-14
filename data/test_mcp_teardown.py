"""MCP stdio teardown: already-exited child is not a scan error."""

from __future__ import annotations

import logging

from data.tv_client import _is_benign_mcp_gone_process, _McpGoneProcessFilter


def test_esrch_killpg_is_benign():
    assert _is_benign_mcp_gone_process(
        "Process group termination failed for PID 486126: "
        "[Errno 3] No such process, falling back to simple terminate"
    )
    assert not _is_benign_mcp_gone_process(
        "Process group termination failed for PID 1: Permission denied"
    )


def test_filter_drops_gone_process_warning(caplog):
    logger = logging.getLogger("mcp.os.posix.utilities")
    filt = _McpGoneProcessFilter()
    logger.addFilter(filt)
    try:
        with caplog.at_level(logging.WARNING, logger="mcp.os.posix.utilities"):
            logger.warning(
                "Process group termination failed for PID 1: "
                "[Errno 3] No such process, falling back to simple terminate"
            )
            logger.warning("some other mcp warning")
        assert "No such process" not in caplog.text
        assert "some other mcp warning" in caplog.text
    finally:
        logger.removeFilter(filt)

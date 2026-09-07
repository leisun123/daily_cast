"""Tests for the optional rotating JSON log file."""

import json
import logging

from dailycast.core.logging import configure_logging


def test_configure_logging_mirrors_json_lines_to_a_rotating_file(tmp_path) -> None:
    """The file handler writes the same structured records the console receives."""
    log_file = tmp_path / "logs" / "dailycast.log"
    original_handlers = logging.getLogger().handlers[:]
    try:
        configure_logging("INFO", file_path=log_file, max_bytes=4096, backup_count=2)
        logging.getLogger().info("persistent-log-probe")
        for handler in logging.getLogger().handlers:
            handler.flush()

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        payload = json.loads(lines[-1])
        assert payload["message"] == "persistent-log-probe"
        assert payload["level"] == "INFO"
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.getLogger().handlers = original_handlers


def test_configure_logging_without_file_keeps_console_only(tmp_path) -> None:
    """No file is created when no log path is configured."""
    original_handlers = logging.getLogger().handlers[:]
    try:
        configure_logging("INFO")
        assert all(
            not isinstance(handler, logging.handlers.RotatingFileHandler)
            for handler in logging.getLogger().handlers
        )
        assert not (tmp_path / "dailycast.log").exists()
    finally:
        logging.getLogger().handlers = original_handlers

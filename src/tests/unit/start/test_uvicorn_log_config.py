"""``_uvicorn_log_config`` (``src/start/workspace.py``) — the root-logger
handler grafted onto uvicorn's own log_config.

The regression: uvicorn's ``Config.configure_logging()`` only wires up its
own loggers (``uvicorn``, ``uvicorn.error``, ``uvicorn.access``) via
``dictConfig`` — it never touches the root logger, so every other module's
``logging.getLogger(__name__)`` call was silently dropped or swallowed by
Python's bare ``lastResort`` handler. This must be threaded through
``log_config=`` (not a bare ``logging.basicConfig()``) so that each
``workers>1`` subprocess, which never re-runs this module's ``main()``,
still gets it via its own ``configure_logging()`` call.

The dict returned is fed straight into ``logging.config.dictConfig`` inside
every uvicorn worker process, so a shared, mutated ``LOGGING_CONFIG`` would
leak formatting/handlers across workers depending on call order — hence the
deepcopy, asserted here by mutating the returned dict and checking the
original import is untouched.
"""
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG

from src.start.workspace import _uvicorn_log_config


def test_attaches_a_root_handler_and_formatter():
    log_config = _uvicorn_log_config()

    assert log_config["root"]["handlers"] == ["root"]
    assert log_config["handlers"]["root"]["class"] == "logging.StreamHandler"
    assert log_config["formatters"]["root"]["format"]


def test_root_level_is_info():
    log_config = _uvicorn_log_config()

    assert log_config["root"]["level"] == "INFO"


def test_does_not_mutate_uvicorns_own_logging_config():
    """The regression this deepcopy guards against: without it, adding the
    "root" formatter/handler/logger below would mutate the shared
    ``uvicorn.config.LOGGING_CONFIG`` module-level dict itself, corrupting it
    for every other consumer (including uvicorn's own internals) in the same
    process."""
    before = deepcopy(LOGGING_CONFIG)

    log_config = _uvicorn_log_config()
    log_config["root"]["level"] = "DEBUG"
    log_config["handlers"]["root"]["class"] = "mutated"
    log_config["formatters"]["root"]["format"] = "mutated"

    assert "root" not in LOGGING_CONFIG["handlers"]
    assert "root" not in LOGGING_CONFIG["formatters"]
    assert LOGGING_CONFIG == before

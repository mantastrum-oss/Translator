"""Test-wide safety net: tests must never reach real external services.

Importing ``bot`` loads the real ``.env`` (production/dev Supabase, Google,
DeepL, analytics keys). Any network call a test forgets to mock would hit those
services, so outbound connections to anything but localhost fail immediately.
Local sockets stay allowed because asyncio uses them internally on Windows.
"""

import socket

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _host_of(address):
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _guard(address):
    host = _host_of(address)
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(f"Network access is disabled in tests (attempted {host}); mock this call.")


def _guarded_connect(self, address):
    _guard(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _guard(address)
    return _real_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    _guard(address)
    return _real_create_connection(address, *args, **kwargs)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)

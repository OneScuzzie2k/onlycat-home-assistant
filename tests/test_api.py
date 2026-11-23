"""Tests for OnlyCat API client."""

from unittest.mock import AsyncMock, Mock

import pytest
from custom_components.onlycat.api import (
    ONLYCAT_NAMESPACE,
    ONLYCAT_URL,
    OnlyCatApiClient,
    OnlyCatApiClientAuthenticationError,
    OnlyCatApiClientError,
    OnlyCatConnectionState,
)
from socketio.exceptions import ConnectionError as SocketIOConnectionError


@pytest.mark.asyncio
async def test_connect_uses_default_namespace_list() -> None:
    """Test connect uses an explicit list for the default namespace."""
    mock_socket = AsyncMock()
    mock_socket.connected = False
    mock_socket.on = Mock()
    client = OnlyCatApiClient("token", AsyncMock(), socket=mock_socket)

    await client.connect()

    mock_socket.connect.assert_awaited_once_with(
        ONLYCAT_URL,
        transports=["websocket"],
        namespaces=[ONLYCAT_NAMESPACE],
        headers={"platform": "home-assistant", "device": "onlycat-hass"},
        auth={"token": "token"},
    )


@pytest.mark.asyncio
async def test_connect_wraps_namespace_failures() -> None:
    """Test connect wraps socket namespace failures with the integration error."""
    mock_socket = AsyncMock()
    mock_socket.connected = False
    mock_socket.on = Mock()
    mock_socket.failed_namespaces = [ONLYCAT_NAMESPACE]
    mock_socket.connect.side_effect = SocketIOConnectionError(
        "One or more namespaces failed to connect"
    )
    client = OnlyCatApiClient("token", AsyncMock(), socket=mock_socket)

    with pytest.raises(OnlyCatApiClientError):
        await client.connect()


def test_clear_event_listeners_deregisters_callbacks() -> None:
    """Test unload cleanup removes callbacks which retain HA entities."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    client = OnlyCatApiClient("api-key", AsyncMock(), socket=mock_socket)
    listener = AsyncMock()
    client.add_event_listener("eventUpdate", listener)

    client.clear_event_listeners()

    assert not client._listeners


@pytest.mark.asyncio
async def test_connection_state_updates_on_disconnect_and_reconnect() -> None:
    """Test live connection-state callbacks publish disconnect and reconnect."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    mock_socket.reconnection = True
    client = OnlyCatApiClient("token", AsyncMock(), socket=mock_socket)
    connection_listener = AsyncMock()
    reconnect_listener = AsyncMock()
    client.add_event_listener("connectionState", connection_listener)
    client.add_event_listener("reconnect", reconnect_listener)

    await client._handle_socket_disconnect("server disconnect")
    await client._on_socket_reconnect_event("reconnect_attempt", 1, 10.0)
    await client._handle_socket_connect()

    assert client.connection_state == OnlyCatConnectionState(
        connected=True,
        phase="reconnect",
        reason=None,
        error=None,
        reconnect_attempt=None,
        reconnect_delay=None,
    )
    assert connection_listener.await_args_list[0].args[0] == OnlyCatConnectionState(
        connected=False,
        phase="disconnect",
        reason="server disconnect",
        error=None,
        reconnect_attempt=None,
        reconnect_delay=None,
    )
    assert connection_listener.await_args_list[1].args[0] == OnlyCatConnectionState(
        connected=False,
        phase="reconnect_attempt",
        reason=None,
        error=None,
        reconnect_attempt=1,
        reconnect_delay=10.0,
    )
    assert connection_listener.await_args_list[2].args[0] == client.connection_state
    reconnect_listener.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_state_updates_on_connect_error_and_reconnect_failure() -> (
    None
):
    """Test failed connection lifecycle updates are published."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    mock_socket.reconnection = True
    client = OnlyCatApiClient("token", AsyncMock(), socket=mock_socket)
    connection_listener = AsyncMock()
    reconnect_failed_listener = AsyncMock()
    client.add_event_listener("connectionState", connection_listener)
    client.add_event_listener("reconnect_failed", reconnect_failed_listener)

    await client._handle_socket_connect_error("namespace rejected")
    await client._on_socket_reconnect_event("reconnect_failed", 3, None)

    assert client.connection_state == OnlyCatConnectionState(
        connected=False,
        phase="reconnect_failed",
        reason=None,
        error=None,
        reconnect_attempt=3,
        reconnect_delay=None,
    )
    assert connection_listener.await_args_list[0].args[0] == OnlyCatConnectionState(
        connected=False,
        phase="connect_error",
        reason=None,
        error="namespace rejected",
        reconnect_attempt=None,
        reconnect_delay=None,
    )
    reconnect_failed_listener.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_restart_connection_marks_requested_and_reconnects() -> None:
    """Test explicit transport recovery is not reported as event staleness."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    mock_socket.connected = True
    mock_socket.reconnection = True
    client = OnlyCatApiClient("token", AsyncMock(), socket=mock_socket)
    connection_listener = AsyncMock()
    client.add_event_listener("connectionState", connection_listener)

    async def disconnect_side_effect() -> None:
        mock_socket.connected = False
        await client._handle_socket_disconnect("forced restart")

    async def connect_side_effect(*args, **kwargs) -> None:
        mock_socket.connected = True
        await client._handle_socket_connect()

    mock_socket.disconnect.side_effect = disconnect_side_effect
    mock_socket.connect.side_effect = connect_side_effect

    result = await client.restart_connection("stale health monitor")

    assert result is True
    assert connection_listener.await_args_list[0].args[0] == OnlyCatConnectionState(
        connected=False,
        phase="restart_requested",
        reason="stale health monitor",
        error=None,
        reconnect_attempt=None,
        reconnect_delay=None,
    )
    assert client.connection_state == OnlyCatConnectionState(
        connected=True,
        phase="reconnect",
        reason=None,
        error=None,
        reconnect_attempt=None,
        reconnect_delay=None,
    )


@pytest.mark.asyncio
async def test_authenticated_call_distinguishes_rejected_credentials() -> None:
    """Test an explicit 401/403 reply is not mistaken for transport failure."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    mock_socket.connected = True
    mock_socket.call.return_value = {"statusCode": 401, "message": "Not logged in"}
    client = OnlyCatApiClient("api-secret", AsyncMock(), socket=mock_socket)

    with pytest.raises(OnlyCatApiClientAuthenticationError):
        await client.send_message("getDevices", {"subscribe": True})

    assert client.is_connected is True
    assert client.authorization_state is False
    assert client.connection_state.authorized is False


@pytest.mark.asyncio
async def test_access_tokens_are_redacted_from_api_logs(caplog) -> None:
    """Test request and response logging cannot disclose media tokens."""
    mock_socket = AsyncMock()
    mock_socket.on = Mock()
    mock_socket.connected = True
    mock_socket.call.return_value = {
        "ok": True,
        "accessToken": "response-secret",
    }
    client = OnlyCatApiClient("api-secret", AsyncMock(), socket=mock_socket)

    with caplog.at_level("DEBUG", logger="custom_components.onlycat.api"):
        await client.send_message(
            "getEventSummary",
            {"eventId": 1, "accessToken": "request-secret"},
        )

    mock_socket.call.assert_awaited_once_with(
        "getEventSummary",
        {"eventId": 1, "accessToken": "request-secret"},
        namespace="/",
    )
    assert "[REDACTED]" in caplog.text
    assert "request-secret" not in caplog.text
    assert "response-secret" not in caplog.text
    assert "api-secret" not in caplog.text

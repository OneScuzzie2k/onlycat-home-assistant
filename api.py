"""OnlyCat API Client."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiohttp

    from .data import OnlyCatData

import socketio
from socketio import base_client as socketio_base_client
from socketio import exceptions as socketio_exceptions
from socketio.exceptions import ConnectionError as SocketIOConnectionError

from .debug_trace import trace_event

_LOGGER = logging.getLogger(__name__)

ONLYCAT_URL = "https://gateway.onlycat.com"
ONLYCAT_NAMESPACE = "/"


class OnlyCatApiClientError(Exception):
    """Exception to indicate a general API error."""


class OnlyCatApiClientCommunicationError(
    OnlyCatApiClientError,
):
    """Exception to indicate a communication error."""


class OnlyCatApiClientAuthenticationError(
    OnlyCatApiClientError,
):
    """Exception to indicate an authentication error."""


@dataclass(slots=True)
class OnlyCatConnectionState:
    """Live Socket.IO connection state for the integration."""

    connected: bool
    phase: str
    reason: str | None = None
    error: str | None = None
    reconnect_attempt: int | None = None
    reconnect_delay: float | None = None
    authorized: bool | None = None


_SENSITIVE_KEYS = {
    "token",
    "accesstoken",
    "access_token",
    "api_key",
    "apikey",
    "authorization",
}


def _redact_sensitive(value: Any) -> Any:
    """Return a recursively redacted copy suitable for logs and diagnostics."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).lower() in _SENSITIVE_KEYS
            else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


def _is_authentication_error(reply: Any) -> bool:
    """Recognize explicit OnlyCat authorization rejection responses."""
    if not isinstance(reply, dict):
        return False
    status_values = {
        reply.get("status"),
        reply.get("statusCode"),
        reply.get("status_code"),
        reply.get("code"),
    }
    if (
        401 in status_values
        or 403 in status_values
        or "401" in status_values
        or "403" in status_values
    ):
        return True
    message = " ".join(
        str(reply.get(key, "")) for key in ("message", "error", "reason")
    ).lower()
    return "not logged in" in message or "access denied" in message


class OnlyCatSocketClient(socketio.AsyncClient):
    """Socket.IO client with reconnect lifecycle callbacks."""

    def __init__(
        self,
        *args: Any,
        reconnect_callback: Callable[[str, int | None, float | None], Awaitable[None]]
        | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the wrapped Socket.IO client."""
        super().__init__(*args, **kwargs)
        self._onlycat_reconnect_callback = reconnect_callback

    async def _notify_reconnect_event(
        self,
        event: str,
        attempt: int | None = None,
        delay: float | None = None,
    ) -> None:
        """Notify the integration about reconnect lifecycle events."""
        if self._onlycat_reconnect_callback is None:
            return
        await self._onlycat_reconnect_callback(event, attempt, delay)

    async def _handle_reconnect(self) -> None:
        """Handle automatic reconnection with integration callbacks."""
        if self._reconnect_abort is None:  # pragma: no cover
            self._reconnect_abort = self.eio.create_event()
        self._reconnect_abort.clear()
        socketio_base_client.reconnecting_clients.append(self)
        attempt_count = 0
        current_delay = self.reconnection_delay
        while True:
            delay = current_delay
            current_delay *= 2
            delay = min(delay, self.reconnection_delay_max)
            delay += self.randomization_factor * (2 * random.random() - 1)
            self.logger.info(
                "Connection failed, new attempt in %.02f seconds",
                delay,
            )
            await self._notify_reconnect_event(
                "reconnect_attempt", attempt_count + 1, delay
            )
            abort = False
            try:
                await asyncio.wait_for(self._reconnect_abort.wait(), delay)
                abort = True
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                abort = True
            if abort:
                self.logger.info("Reconnect task aborted")
                for namespace in self.connection_namespaces:
                    await self._trigger_event("__disconnect_final", namespace)
                break
            attempt_count += 1
            try:
                await self.connect(
                    self.connection_url,
                    headers=self.connection_headers,
                    auth=self.connection_auth,
                    transports=self.connection_transports,
                    namespaces=self.connection_namespaces,
                    socketio_path=self.socketio_path,
                    retry=False,
                )
            except (socketio_exceptions.ConnectionError, ValueError):
                pass
            else:
                self.logger.info("Reconnection successful")
                self._reconnect_task = None
                await self._notify_reconnect_event("reconnect", attempt_count, None)
                break
            if (
                self.reconnection_attempts
                and attempt_count >= self.reconnection_attempts
            ):
                self.logger.info("Maximum reconnection attempts reached, giving up")
                await self._notify_reconnect_event(
                    "reconnect_failed", attempt_count, None
                )
                for namespace in self.connection_namespaces:
                    await self._trigger_event("__disconnect_final", namespace)
                break
        socketio_base_client.reconnecting_clients.remove(self)


class OnlyCatApiClient:
    """Only Cat API Client."""

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession,
        data: OnlyCatData | None = None,
        socket: socketio.AsyncClient | None = None,
    ) -> None:
        """Sample API Client."""
        self._api_key = api_key
        self._data = data
        self._session = session
        self._listeners = defaultdict(list)
        self._socket = socket or OnlyCatSocketClient(
            http_session=self._session,
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=10,
            reconnection_delay_max=10,
            ssl_verify=True,
            reconnect_callback=self._on_socket_reconnect_event,
        )
        self._last_connection_state = OnlyCatConnectionState(
            connected=False,
            phase="initial",
        )
        self._reconnect_in_progress = False
        self._restart_lock = asyncio.Lock()
        self._authorized: bool | None = None
        self._socket.on("*", self.handle_event)
        self._socket.on("connect", self._handle_socket_connect)
        self._socket.on("disconnect", self._handle_socket_disconnect)
        self._socket.on("connect_error", self._handle_socket_connect_error)
        self.add_event_listener("connect", self.on_connected)

    async def connect(self) -> None:
        """Connect to wesocket client."""
        if self._socket.connected:
            return
        _LOGGER.debug(
            "Connecting to OnlyCat API at %s using namespace %s",
            ONLYCAT_URL,
            ONLYCAT_NAMESPACE,
        )

        try:
            await self._socket.connect(
                ONLYCAT_URL,
                transports=["websocket"],
                namespaces=[ONLYCAT_NAMESPACE],
                headers={"platform": "home-assistant", "device": "onlycat-hass"},
                auth={"token": self._api_key},
            )
        except SocketIOConnectionError as exception:
            failed_namespaces = getattr(self._socket, "failed_namespaces", None)
            _LOGGER.warning(
                "OnlyCat Socket.IO connect failed for %s namespace %s"
                " (failed_namespaces=%s): %s",
                ONLYCAT_URL,
                ONLYCAT_NAMESPACE,
                failed_namespaces,
                exception,
            )
            raise OnlyCatApiClientError from exception
        except Exception as exception:
            _LOGGER.exception(
                "Unexpected OnlyCat Socket.IO connect error for %s namespace %s",
                ONLYCAT_URL,
                ONLYCAT_NAMESPACE,
            )
            raise OnlyCatApiClientError from exception

    async def disconnect(self) -> None:
        """Disconnect websocket client."""
        _LOGGER.debug("Disconnecting from API")
        if self._socket.connected:
            await self._socket.disconnect()
        await self._socket.shutdown()

    def _namespace_is_connected(self) -> bool:
        """Return whether the OnlyCat namespace appears connected."""
        namespaces = getattr(self._socket, "namespaces", {}) or {}
        return ONLYCAT_NAMESPACE in namespaces

    def _socket_is_ready(self) -> bool:
        """Return whether the socket is ready for Socket.IO calls."""
        return self._socket.connected or self._namespace_is_connected()

    @property
    def is_connected(self) -> bool:
        """Return whether the socket client is currently connected."""
        return self._socket_is_ready()

    def add_event_listener(self, event: str, callback: Any) -> None:
        """Add an event listener."""
        self._listeners[event].append(callback)
        _LOGGER.debug(
            "Added event listener for event %s: %s", event, callback.__module__
        )

    def clear_event_listeners(self) -> None:
        """Deregister callbacks which may reference unloaded HA entities."""
        self._listeners.clear()

    async def handle_event(self, event: str, *args: Any) -> None:
        """Handle an event."""
        safe_args = _redact_sensitive(args)
        _LOGGER.debug("Received event: %s with args: %s", event, safe_args)
        trace_event("socket_event", event=event, args=safe_args)
        for callback in self._listeners[event]:
            try:
                await callback(*args)
            except Exception:
                _LOGGER.exception(
                    "Error while handling event %s with args %s", event, args
                )

    @property
    def connection_state(self) -> OnlyCatConnectionState:
        """Return the last observed socket connection state."""
        return self._last_connection_state

    @property
    def authorization_state(self) -> bool | None:
        """Return authorization established by an authenticated API reply."""
        return self._authorized

    async def _set_authorization_state(self, authorized: bool) -> None:
        """Publish authorization separately from transport connectivity."""
        if self._authorized == authorized:
            return
        self._authorized = authorized
        self._last_connection_state = replace(
            self._last_connection_state,
            authorized=authorized,
        )
        await self.handle_event("connectionState", self._last_connection_state)

    async def wait_until_ready(self, attempts: int = 20, delay: float = 0.5) -> bool:
        """Wait until the socket looks ready for Socket.IO calls."""
        for attempt in range(1, attempts + 1):
            if self._socket_is_ready():
                return True
            _LOGGER.debug(
                "OnlyCat socket not ready yet (attempt %s/%s, connected=%s, namespaces=%s)",
                attempt,
                attempts,
                self._socket.connected,
                getattr(self._socket, "namespaces", {}),
            )
            await asyncio.sleep(delay)
        return self._socket_is_ready()

    async def _emit_connection_state(
        self,
        connected: bool,
        phase: str,
        reason: str | None = None,
        error: str | None = None,
        reconnect_attempt: int | None = None,
        reconnect_delay: float | None = None,
    ) -> None:
        """Store and publish the current Socket.IO connection state."""
        self._last_connection_state = OnlyCatConnectionState(
            connected=connected,
            phase=phase,
            reason=reason,
            error=error,
            reconnect_attempt=reconnect_attempt,
            reconnect_delay=reconnect_delay,
            authorized=self._authorized,
        )
        await self.handle_event("connectionState", self._last_connection_state)

    async def publish_connection_state(
        self,
        connected: bool,
        phase: str,
        reason: str | None = None,
        error: str | None = None,
        reconnect_attempt: int | None = None,
        reconnect_delay: float | None = None,
    ) -> None:
        """Publish a synthetic connection-state update."""
        await self._emit_connection_state(
            connected=connected,
            phase=phase,
            reason=reason,
            error=error,
            reconnect_attempt=reconnect_attempt,
            reconnect_delay=reconnect_delay,
        )

    async def restart_connection(self, reason: str) -> bool:
        """Restart the socket connection and report whether it succeeded."""
        async with self._restart_lock:
            if self._socket.connected:
                _LOGGER.warning("Restarting OnlyCat socket connection: %s", reason)
                await self._emit_connection_state(
                    connected=False,
                    phase="restart_requested",
                    reason=reason,
                )
                try:
                    await self._socket.disconnect()
                except Exception:
                    _LOGGER.exception(
                        "Error while disconnecting stale OnlyCat socket connection"
                    )
            else:
                _LOGGER.warning(
                    "Attempting OnlyCat socket reconnect while disconnected: %s",
                    reason,
                )
                await self._emit_connection_state(
                    connected=False,
                    phase="reconnect_requested",
                    reason=reason,
                )
            try:
                await self.connect()
            except OnlyCatApiClientError:
                _LOGGER.warning(
                    "OnlyCat socket restart failed after connection issue: %s",
                    reason,
                )
                return False
            _LOGGER.info("OnlyCat socket restart succeeded: %s", reason)
            return True

    async def _handle_socket_connect(self) -> None:
        """Handle a Socket.IO connect event."""
        phase = "reconnect" if self._reconnect_in_progress else "connect"
        self._reconnect_in_progress = False
        _LOGGER.info("OnlyCat Socket.IO %s succeeded", phase)
        await self._emit_connection_state(connected=True, phase=phase)
        await self.handle_event("connect")

    async def _handle_socket_disconnect(self, *args: Any) -> None:
        """Handle a Socket.IO disconnect event."""
        reason = str(args[0]) if args else None
        self._reconnect_in_progress = bool(self._socket.reconnection)
        _LOGGER.warning("OnlyCat Socket.IO disconnected (reason=%s)", reason)
        await self._emit_connection_state(
            connected=False,
            phase="disconnect",
            reason=reason,
        )
        await self.handle_event("disconnect", *args)

    async def _handle_socket_connect_error(self, *args: Any) -> None:
        """Handle a Socket.IO connect error event."""
        error = str(args[0]) if args else None
        _LOGGER.warning("OnlyCat Socket.IO connect error: %s", error)
        await self._emit_connection_state(
            connected=False,
            phase="connect_error",
            error=error,
        )
        await self.handle_event("connect_error", *args)

    async def _on_socket_reconnect_event(
        self,
        event: str,
        attempt: int | None,
        delay: float | None,
    ) -> None:
        """Handle reconnect lifecycle callbacks from the socket client."""
        if event == "reconnect_attempt":
            self._reconnect_in_progress = True
            _LOGGER.info(
                "OnlyCat Socket.IO reconnect attempt %s scheduled in %.02f seconds",
                attempt,
                delay if delay is not None else 0,
            )
            await self._emit_connection_state(
                connected=False,
                phase=event,
                reconnect_attempt=attempt,
                reconnect_delay=delay,
            )
        elif event == "reconnect":
            _LOGGER.info(
                "OnlyCat Socket.IO reconnect attempt %s succeeded",
                attempt,
            )
            await self.handle_event(event, attempt)
        elif event == "reconnect_failed":
            self._reconnect_in_progress = False
            _LOGGER.error(
                "OnlyCat Socket.IO reconnect failed after %s attempts",
                attempt,
            )
            await self._emit_connection_state(
                connected=False,
                phase=event,
                reconnect_attempt=attempt,
            )
            await self.handle_event(event, attempt)

    async def send_message(
        self, event: str, data: any, *, notify_listeners: bool = True
    ) -> Any | None:
        """Send a message to the API."""
        _LOGGER.debug(
            "Sending message to API - Event: %s, Data: %s, Data Type: %s",
            event,
            _redact_sensitive(data),
            type(data),
        )
        trace_event("api_call", event=event, data=_redact_sensitive(data))
        last_exception: Exception | None = None
        for attempt in range(1, 7):
            if not self._socket_is_ready():
                _LOGGER.warning(
                    "OnlyCat socket not ready for %s attempt %s/6 (connected=%s, namespaces=%s)",
                    event,
                    attempt,
                    self._socket.connected,
                    getattr(self._socket, "namespaces", {}),
                )
                await asyncio.sleep(0.5)
                continue
            try:
                reply = await self._socket.call(
                    event,
                    data,
                    namespace=ONLYCAT_NAMESPACE,
                )
                break
            except Exception as exception:  # noqa: BLE001 - socketio raises varied errors
                last_exception = exception
                _LOGGER.warning(
                    "OnlyCat socket.call failed for %s on attempt %s/6: %s",
                    event,
                    attempt,
                    exception,
                )
                await asyncio.sleep(0.5)
        else:
            if last_exception is not None:
                _LOGGER.exception(
                    "Error during socket.call for event %s with data %s",
                    event,
                    _redact_sensitive(data),
                    exc_info=last_exception,
                )
                raise OnlyCatApiClientCommunicationError from last_exception
            raise OnlyCatApiClientCommunicationError("OnlyCat socket is not connected")
        safe_reply = _redact_sensitive(reply)
        _LOGGER.debug("Received reply for event %s: %s", event, safe_reply)
        trace_event("api_reply", event=event, reply=safe_reply)
        if reply is None:
            return None
        if _is_authentication_error(reply):
            await self._set_authorization_state(False)
            raise OnlyCatApiClientAuthenticationError(
                f"OnlyCat rejected authenticated request {event}"
            )
        if event.startswith("get"):
            await self._set_authorization_state(True)
        for callback in self._listeners[event] if notify_listeners else ():
            try:
                await callback(reply)
            except Exception:
                _LOGGER.exception(
                    "Error while handling reply for event %s with data %s: %s",
                    event,
                    _redact_sensitive(data),
                    safe_reply,
                )
        return reply

    async def wait(self) -> None:
        """Wait until client is disconnected."""
        await self._socket.wait()

    async def on_connected(self) -> None:
        """Handle connected event."""
        _LOGGER.debug("(Re)connected to API")

"""Adds config flow for OnlyCat."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    OnlyCatApiClient,
    OnlyCatApiClientAuthenticationError,
    OnlyCatApiClientCommunicationError,
    OnlyCatApiClientError,
)
from .const import (
    CONF_API_KEY,
    CONF_ENABLE_DEBUG_LOGGING,
    CONF_ENABLE_DETAILED_METRICS,
    CONF_EVENT_RETRY_DELAYS_SECONDS,
    CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS,
    CONF_IGNORE_FLAP_MOTION_RULES,
    CONF_IGNORE_MOTION_SENSOR_RULES,
    CONF_POLL_INTERVAL_HOURS,
    DOMAIN,
    LOGGER,
    ONLYCAT_API_STATUS_CODES,
)

_LOGGER = logging.getLogger(__name__)


class OnlyCatFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for OnlyCat."""

    # Version 3 is already deployed by our custom branch.  Use version 4 so
    # those entries run the token -> api_key migration from __init__.py.
    VERSION = 4

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            try:
                _LOGGER.debug("Initializing API client")
                client = OnlyCatApiClient(
                    user_input[CONF_API_KEY],
                    session=async_create_clientsession(self.hass),
                )
                user_id = None

                async def on_user_update(data: any) -> None:
                    nonlocal user_id
                    if data is not None and "id" in data:
                        user_id = str(data["id"])

                settings = {}
                settings["ignore_flap_motion_rules"] = user_input[
                    CONF_IGNORE_FLAP_MOTION_RULES
                ]
                settings["ignore_motion_sensor_rules"] = user_input[
                    CONF_IGNORE_MOTION_SENSOR_RULES
                ]
                settings["poll_interval_hours"] = user_input[CONF_POLL_INTERVAL_HOURS]
                settings["enable_detailed_metrics"] = user_input.get(
                    CONF_ENABLE_DETAILED_METRICS, False
                )
                settings["enable_debug_logging"] = user_input.get(
                    CONF_ENABLE_DEBUG_LOGGING, False
                )
                settings["event_retry_delays_seconds"] = user_input.get(
                    CONF_EVENT_RETRY_DELAYS_SECONDS, "1,3,6"
                )
                settings["final_event_retry_delays_seconds"] = user_input.get(
                    CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS, "0,1,3"
                )
                client.add_event_listener("userUpdate", on_user_update)
                await self._validate_connection(client)
            except OnlyCatApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                _errors["base"] = "auth"
            except OnlyCatApiClientCommunicationError as exception:
                LOGGER.error(exception)
                _errors["base"] = "connection"
            except OnlyCatApiClientError as exception:
                LOGGER.exception(exception)
                _errors["base"] = "unknown"
            else:
                _LOGGER.debug("Creating entry with id %s", user_id)
                await self.async_set_unique_id(unique_id=user_id)
                self._abort_if_unique_id_configured()
                return_data = {
                    "user_id": user_id,
                    "api_key": user_input[CONF_API_KEY],
                    "settings": settings,
                }
                return self.async_create_entry(
                    title=user_id,
                    data=return_data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_API_KEY,
                        default=(user_input or {}).get(
                            CONF_API_KEY, vol.UNDEFINED
                        ),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                        ),
                    ),
                    vol.Required(
                        CONF_IGNORE_FLAP_MOTION_RULES,
                        default=(user_input or {}).get(
                            CONF_IGNORE_FLAP_MOTION_RULES, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_IGNORE_MOTION_SENSOR_RULES,
                        default=(user_input or {}).get(
                            CONF_IGNORE_MOTION_SENSOR_RULES, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_POLL_INTERVAL_HOURS,
                        default=(user_input or {}).get(CONF_POLL_INTERVAL_HOURS, 1),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.25,
                            max=24,
                            step=0.25,
                        )
                    ),
                    vol.Optional(
                        CONF_ENABLE_DETAILED_METRICS,
                        default=(user_input or {}).get(
                            CONF_ENABLE_DETAILED_METRICS, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_ENABLE_DEBUG_LOGGING,
                        default=(user_input or {}).get(
                            CONF_ENABLE_DEBUG_LOGGING, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EVENT_RETRY_DELAYS_SECONDS,
                        default=(user_input or {}).get(
                            CONF_EVENT_RETRY_DELAYS_SECONDS, "1,3,6"
                        ),
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS,
                        default=(user_input or {}).get(
                            CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS, "0,1,3"
                        ),
                    ): selector.TextSelector(),
                },
            ),
            errors=_errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle a reconfiguration flow."""
        errors: dict[str, str] = {}
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        settings = {
            "ignore_flap_motion_rules": False,
            "ignore_motion_sensor_rules": False,
            "poll_interval_hours": 1,
            "enable_detailed_metrics": False,
            "enable_debug_logging": False,
            "event_retry_delays_seconds": "1,3,6",
            "final_event_retry_delays_seconds": "0,1,3",
        }
        if user_input is not None and config_entry is not None:
            settings["ignore_flap_motion_rules"] = user_input[
                CONF_IGNORE_FLAP_MOTION_RULES
            ]
            settings["ignore_motion_sensor_rules"] = user_input[
                CONF_IGNORE_MOTION_SENSOR_RULES
            ]
            settings["poll_interval_hours"] = user_input[CONF_POLL_INTERVAL_HOURS]
            settings["enable_detailed_metrics"] = user_input.get(
                CONF_ENABLE_DETAILED_METRICS, False
            )
            settings["enable_debug_logging"] = user_input.get(
                CONF_ENABLE_DEBUG_LOGGING, False
            )
            settings["event_retry_delays_seconds"] = user_input.get(
                CONF_EVENT_RETRY_DELAYS_SECONDS, "1,3,6"
            )
            settings["final_event_retry_delays_seconds"] = user_input.get(
                CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS, "0,1,3"
            )
            return self.async_update_reload_and_abort(
                config_entry,
                unique_id=config_entry.unique_id,
                data={
                    "user_id": config_entry.data["user_id"],
                    "api_key": config_entry.data["api_key"],
                    "settings": settings,
                },
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IGNORE_FLAP_MOTION_RULES,
                        default=(user_input or {}).get(
                            CONF_IGNORE_FLAP_MOTION_RULES, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_IGNORE_MOTION_SENSOR_RULES,
                        default=(user_input or {}).get(
                            CONF_IGNORE_MOTION_SENSOR_RULES, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_POLL_INTERVAL_HOURS,
                        default=(user_input or {}).get(CONF_POLL_INTERVAL_HOURS, 6),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.25,
                            max=24,
                            step=0.25,
                        )
                    ),
                    vol.Optional(
                        CONF_ENABLE_DETAILED_METRICS,
                        default=(user_input or {}).get(
                            CONF_ENABLE_DETAILED_METRICS,
                            config_entry.data.get("settings", {}).get(
                                CONF_ENABLE_DETAILED_METRICS, False
                            )
                            if config_entry is not None
                            else False,
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_ENABLE_DEBUG_LOGGING,
                        default=(user_input or {}).get(
                            CONF_ENABLE_DEBUG_LOGGING,
                            config_entry.data.get("settings", {}).get(
                                CONF_ENABLE_DEBUG_LOGGING, False
                            )
                            if config_entry is not None
                            else False,
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EVENT_RETRY_DELAYS_SECONDS,
                        default=(user_input or {}).get(
                            CONF_EVENT_RETRY_DELAYS_SECONDS,
                            config_entry.data.get("settings", {}).get(
                                CONF_EVENT_RETRY_DELAYS_SECONDS, "1,3,6"
                            )
                            if config_entry is not None
                            else "1,3,6",
                        ),
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS,
                        default=(user_input or {}).get(
                            CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS,
                            config_entry.data.get("settings", {}).get(
                                CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS, "0,1,3"
                            )
                            if config_entry is not None
                            else "0,1,3",
                        ),
                    ): selector.TextSelector(),
                },
            ),
            errors=errors,
        )

    async def _validate_connection(self, client: OnlyCatApiClient) -> None:
        """Validate connection."""
        await client.connect()
        response = await client.send_message("getDevices", {"subscribe": False})
        if (
            type(response) is dict
            and "code" in response
            and response["code"] == ONLYCAT_API_STATUS_CODES["UNAUTHORIZED"]
        ):
            error_msg = "Invalid API key"
            raise OnlyCatApiClientAuthenticationError(error_msg)
        await client.disconnect()

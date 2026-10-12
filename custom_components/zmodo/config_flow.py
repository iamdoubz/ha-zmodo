"""Config flow for Zmodo integration — app-style login (no captcha)."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ZmodoApi, stable_client_uuid
from .platform_api import ZmodoPlatformApi, ZmodoPlatformAuthError
from .const import (
    CONF_ALARM_ADDRESSES,
    CONF_APP_ADDRESSES,
    CONF_CLIENT_UUID,
    CONF_LOGIN_CERT,
    CONF_MNG_ADDRESSES,
    CONF_PLATFORM_USER_ID,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)


class ZmodoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-step config flow: email + password → done."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect credentials and attempt login immediately."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input["email"]
            password = user_input["password"]
            client_uuid = stable_client_uuid()

            session = async_get_clientsession(self.hass)
            platform = ZmodoPlatformApi(session, email, password)

            try:
                await platform.login()
            except ZmodoPlatformAuthError as err:
                _LOGGER.warning("Zmodo login failed: %s", err)
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError as err:
                _LOGGER.error("Zmodo connection error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Zmodo login: %s", err)
                errors["base"] = "unknown"
            else:
                entry_data: dict[str, Any] = {
                    "email": email,
                    "password": password,
                    CONF_CLIENT_UUID: client_uuid,
                    CONF_PLATFORM_USER_ID: platform.user_id,
                }
                unique_id = platform.user_id or email.lower()

                # Legacy MeShare login is optional: it only powers the
                # settings entities. The coordinator retries it later.
                try:
                    legacy = await ZmodoApi(session).login(
                        email=email, password_plain=password, client_uuid=client_uuid
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.info("Legacy Zmodo login unavailable: %s", err)
                else:
                    host_list = legacy.get("host_list", {})
                    unique_id = legacy["data"]["id"]  # matches pre-2026.10 entries
                    entry_data.update({
                        CONF_TOKEN: legacy["token"],
                        CONF_LOGIN_CERT: legacy.get("login_cert", ""),
                        CONF_USER_ID: legacy["data"]["id"],
                        CONF_APP_ADDRESSES: host_list.get("app_address", []),
                        CONF_ALARM_ADDRESSES: host_list.get("alarm_address", []),
                        CONF_MNG_ADDRESSES: host_list.get("mng_address", []),
                    })

                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=email, data=entry_data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

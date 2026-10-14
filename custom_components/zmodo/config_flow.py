"""Config flow for Zmodo integration — app-style login (no captcha)."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ZmodoApi, stable_client_uuid
from .platform_api import ZmodoPlatformApi, ZmodoPlatformAuthError
from .const import (
    CONF_ALARM_ADDRESSES,
    CONF_APP_ADDRESSES,
    CONF_CLIENT_UUID,
    CONF_LOCAL_STREAMS,
    CONF_LOGIN_CERT,
    CONF_MNG_ADDRESSES,
    CONF_PLATFORM_USER_ID,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    OPT_LOCAL_HOST,
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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "ZmodoOptionsFlow":
        return ZmodoOptionsFlow(config_entry)

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


class ZmodoOptionsFlow(config_entries.OptionsFlow):
    """Per-camera options: direct-LAN stream host, for low-latency local view.

    Setting a host enables the fast local path for the SD entity and adds a
    local-only HD entity for that camera.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Private attr, not self.config_entry: recent HA provides that as a
        # read-only property and assigning it is deprecated.
        self._entry = config_entry

    def _cameras(self) -> list[tuple[str, str]]:
        """(serial, label) for each camera, from the running coordinator."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        data = getattr(coordinator, "data", None) or {}
        out = []
        for serial, dev in data.get("devices", {}).items():
            out.append((serial, dev.get("device_name") or serial))
        return out

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        cameras = self._cameras()
        current: dict[str, Any] = dict(
            self._entry.options.get(CONF_LOCAL_STREAMS, {})
        )

        if user_input is not None:
            local: dict[str, Any] = {}
            for serial, _ in cameras:
                host = (user_input.get(f"{serial}__host") or "").strip()
                if host:
                    local[serial] = {OPT_LOCAL_HOST: host}
            return self.async_create_entry(
                title="", data={CONF_LOCAL_STREAMS: local}
            )

        if not cameras:
            return self.async_abort(reason="no_cameras")

        fields: dict[Any, Any] = {}
        for serial, label in cameras:
            cam = current.get(serial, {})
            fields[
                vol.Optional(
                    f"{serial}__host",
                    description={"suggested_value": cam.get(OPT_LOCAL_HOST, "")},
                )
            ] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "cameras": ", ".join(label for _, label in cameras)
            },
        )

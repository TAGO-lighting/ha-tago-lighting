"""Config flow for Tago integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import zeroconf
from homeassistant.data_entry_flow import FlowResult

from .TagoNet import TagoGateway, log_connection_diagnostic

from .const import (
    CONF_DEVICENAME,
    CONF_HOSTSTR,
    CONF_PIN,
    LEGACY_TLS_CONFIG_KEYS,
    DOMAIN,
)


_LOGGER = logging.getLogger(__name__)


class TagoConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    # Bumped to 9: credential storage key renamed `authkey` -> `pin`.
    # See custom_components/tago/__init__.py::async_migrate_entry.
    VERSION = 9

    def __init__(self):
        self.errors: dict[str, str] = {}
        self.device_name: str | None = None
        self.device_serial: str | None = None
        self.discovered_address: str = ""
        self.hoststr: str | None = None
        self.pin: str = ""
        # Set during reauth flow.
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.pin = user_input.get(CONF_PIN, "").strip()
            self.hoststr = user_input[CONF_HOSTSTR].strip()
            return await self.async_step_test_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOSTSTR): str,
                    vol.Optional(CONF_PIN): str,
                }
            ),
            errors=self.errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        if discovery_info:
            host = discovery_info.host
            self.discovered_address = host
            hostname = discovery_info.hostname.rstrip('.').strip()
            self.hoststr = f"{hostname}:{discovery_info.port}"
            self.device_serial = discovery_info.properties.get('serialnum', 'UNKNOWN')
            location = discovery_info.properties.get('location', '').strip()
            self.device_name = f"{self.device_serial} ({location})" if location else self.device_serial

            await self.async_set_unique_id(self.device_serial)
            # Gold-tier rule `discovery-update-info`: when a re-discovery
            # lands for an already-configured device, refresh the stored
            # hostname/port so the entry heals automatically if the
            # device moved networks or changed port.
            self._abort_if_unique_id_configured(
                updates={CONF_HOSTSTR: self.hoststr},
            )

            self.context.update(
                {
                    "title_placeholders": {
                        "device_name": self.device_name
                    }
                }
            )

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm zeroconf configuration."""
        if user_input is not None:
            self.pin = user_input.get(CONF_PIN, '').strip()
            return await self.async_step_test_connection()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PIN): str,
                }
            ),
            description_placeholders={
                CONF_DEVICENAME: self.device_name,
                "ip_address": self.discovered_address,
            },
            errors=self.errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the user to change hostname/PIN on an already-configured
        entry. Triggered from the device's "Reconfigure" menu."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            new_host = user_input[CONF_HOSTSTR].strip()
            new_pin = user_input.get(CONF_PIN, "").strip()
            self.errors = {}
            try:
                await TagoGateway(new_host, new_pin, useSSL=True, verifySSL=True).connect_and_auth(timeout=5.0)
            except PermissionError as err:
                log_connection_diagnostic(new_host, "Reconfiguration authentication failed", error=err)
                self.errors["base"] = "invalid_auth"
            except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError, ValueError) as err:
                log_connection_diagnostic(new_host, "Reconfiguration could not connect", error=err)
                self.errors["base"] = "cannot_connect"

            if not self.errors:
                new_data = {
                    **{key: value for key, value in entry.data.items() if key not in LEGACY_TLS_CONFIG_KEYS},
                    CONF_HOSTSTR: new_host,
                    CONF_PIN: new_pin,
                }
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOSTSTR,
                                 default=entry.data.get(CONF_HOSTSTR, "")): str,
                    vol.Optional(CONF_PIN,
                                 default=entry.data.get(CONF_PIN, "")): str,
                }
            ),
            errors=self.errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Start a reauth flow: stored credentials stopped working
        (typically because the device's firmware was upgraded to the
        PIN-based auth model and the saved api_key no longer authenticates)."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")
        self.hoststr = entry_data.get(CONF_HOSTSTR) or self._reauth_entry.data.get(CONF_HOSTSTR)
        self.device_name = self._reauth_entry.unique_id or "Tago Device"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Prompt the user for the device PIN and re-test the connection."""
        if user_input is not None:
            self.pin = user_input.get(CONF_PIN, "").strip()
            self.errors = {}
            try:
                await TagoGateway(self.hoststr, self.pin, useSSL=True, verifySSL=True).connect_and_auth(timeout=5.0)
            except PermissionError as err:
                log_connection_diagnostic(self.hoststr, "Reauthentication failed", error=err)
                self.errors["base"] = "invalid_auth"
            except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError, ValueError) as err:
                log_connection_diagnostic(self.hoststr, "Reauthentication could not connect", error=err)
                self.errors["base"] = "cannot_connect"

            if not self.errors and self._reauth_entry is not None:
                new_data = {
                    **{key: value for key, value in self._reauth_entry.data.items() if key not in LEGACY_TLS_CONFIG_KEYS},
                    CONF_PIN: self.pin,
                }
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=new_data
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN): str}),
            description_placeholders={CONF_DEVICENAME: self.device_name or "Tago Device"},
            errors=self.errors,
        )

    async def async_step_test_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Probe the gateway: just verify host reachability + PIN auth.
        Per-device discovery is deferred to entry setup."""
        self.errors = {}
        try:
            await TagoGateway(self.hoststr, self.pin, useSSL=True, verifySSL=True).connect_and_auth(timeout=5.0)
        except PermissionError as e:
            log_connection_diagnostic(self.hoststr, "Setup authentication failed; showing invalid_auth", error=e)
            _LOGGER.debug("Authentication failed: %s", str(e))
            self.errors["base"] = "invalid_auth"

            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()
        except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError, ValueError) as e:
            log_connection_diagnostic(self.hoststr, "Setup connection failed; showing cannot_connect", error=e)
            _LOGGER.debug("Connection failed: %s", str(e))
            self.errors["base"] = "cannot_connect"

            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()

        await self.async_set_unique_id(self.device_serial or self.hoststr)
        self._abort_if_unique_id_configured()

        _LOGGER.debug("Successfully connected to Tago gateway %s", self.hoststr)
        return self.async_create_entry(
            title=self.device_name if self.device_serial else f'TAGO Gateway @ {self.hoststr}',
            data={
                CONF_PIN: self.pin,
                CONF_DEVICENAME: self.device_name if self.device_serial else self.hoststr,
                CONF_HOSTSTR: self.hoststr,
            },
        )

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from .installer import build_clone_url, _run
from homeassistant import config_entries
from .const import (
	CONF_ARCHIVE_SUBDIR,
	CONF_AUTO_UPDATE,
	CONF_COMPONENT_NAME,
	CONF_COMPONENTS,
	CONF_DEST_TYPE,
	CONF_SERVER_URL,
	CONF_MODE,
	CONF_PROJECT_PATH,
	CONF_REF,
	CONF_TOKEN,
	CONF_TOKEN_USERNAME,
	DEFAULT_SERVER_URL,
	DEST_TYPE_CUSTOM_COMPONENT,
	DEST_TYPE_WWW,
	DOMAIN,
	MODE_BRANCH,
	MODE_TAG,
)

_LOGGER = logging.getLogger(__name__)


async def _install_when_ready(hass, component_name: str) -> None:
	"""Install a freshly-added component once the options reload registers it.

	async_create_entry reloads the entry asynchronously; until that finishes the
	coordinator does not list the new component and deployer.install raises "not
	found in any configured entry". The old code used a fixed 5s timer that raced
	the reload; poll for up to ~30s instead so the timing can never lose the race.
	"""
	for _ in range(30):
		for coord in hass.data.get(DOMAIN, {}).values():
			if component_name in [c[CONF_COMPONENT_NAME] for c in coord.components]:
				try:
					await hass.services.async_call(
						DOMAIN, "install", {"component_name": component_name}, blocking=True
					)
				except Exception as err:  # noqa: BLE001 — surface install failures instead of leaking
					_LOGGER.error("Deployer: auto-install of %s failed: %s", component_name, err)
				return
		await asyncio.sleep(1)
	_LOGGER.error(
		"Deployer: %s was not registered within 30s of being added; skipping "
		"auto-install. Run the deployer.install service manually once it appears.",
		component_name,
	)


def _component_schema(defaults: dict | None = None) -> vol.Schema:
	d = defaults or {}
	return vol.Schema({
		vol.Required(CONF_PROJECT_PATH, default=d.get(CONF_PROJECT_PATH, "")): str,
		vol.Required(CONF_COMPONENT_NAME, default=d.get(CONF_COMPONENT_NAME, "")): str,
		vol.Required(CONF_MODE, default=d.get(CONF_MODE, MODE_BRANCH)): vol.In([MODE_BRANCH, MODE_TAG]),
		vol.Required(CONF_REF, default=d.get(CONF_REF, "main")): str,
		vol.Optional(CONF_ARCHIVE_SUBDIR, default=d.get(CONF_ARCHIVE_SUBDIR, "")): str,
		vol.Required(CONF_DEST_TYPE, default=d.get(CONF_DEST_TYPE, DEST_TYPE_CUSTOM_COMPONENT)): vol.In([DEST_TYPE_CUSTOM_COMPONENT, DEST_TYPE_WWW]),
		vol.Optional(CONF_AUTO_UPDATE, default=d.get(CONF_AUTO_UPDATE, False)): bool,
	})


async def _test_server_reachable(hass, server_url: str) -> str | None:
	"""Confirm the git server hostname resolves and responds (platform-agnostic)."""
	import aiohttp
	from homeassistant.helpers.aiohttp_client import async_get_clientsession
	session = async_get_clientsession(hass)
	try:
		async with session.get(
			server_url,
			timeout=aiohttp.ClientTimeout(total=10),
			allow_redirects=True,
		) as resp:
			if resp.status >= 500:
				return "cannot_connect"
	except Exception as err:
		_LOGGER.debug("Server reachability test failed: %s", err)
		return "cannot_connect"
	return None


async def _test_project_access(
	server_url: str, token: str, token_username: str, project_path: str
) -> str | None:
	"""Verify token can access the repo using git ls-remote (works for group deploy tokens)."""
	clone_url = build_clone_url(server_url, token, token_username, project_path)
	try:
		rc, stdout, stderr = await _run(["git", "ls-remote", clone_url, "HEAD"], timeout=15)
		if rc != 0:
			err_lower = stderr.lower()
			if "authentication" in err_lower or "not found" in err_lower or "403" in err_lower:
				return "invalid_token"
			if "repository" in err_lower and "not found" in err_lower:
				return "project_not_found"
			return "project_not_found"
	except Exception as err:
		_LOGGER.debug("Project access test failed: %s", err)
		return "cannot_connect"
	return None


class ComponentUpdaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
	VERSION = 1

	async def async_step_user(self, user_input: dict[str, Any] | None = None):
		errors: dict[str, str] = {}

		if user_input is not None:
			server_url = user_input[CONF_SERVER_URL].rstrip("/")
			token = user_input.get(CONF_TOKEN, "")
			token_username = user_input.get(CONF_TOKEN_USERNAME, "").strip()
			error = await _test_server_reachable(self.hass, server_url)
			if error:
				errors["base"] = error
			else:
				return self.async_create_entry(
					title=user_input.get("name", server_url),
					data={
						"name": user_input.get("name", ""),
						CONF_SERVER_URL: server_url,
						CONF_TOKEN: token,
						CONF_TOKEN_USERNAME: token_username,
					},
					options={CONF_COMPONENTS: []},
				)

		return self.async_show_form(
			step_id="user",
			data_schema=vol.Schema({
				vol.Required("name", default="My Git Forge"): str,
				vol.Required(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): str,
				vol.Optional(CONF_TOKEN_USERNAME, default=""): str,
				vol.Optional(CONF_TOKEN, default=""): str,
			}),
			description_placeholders={},
			errors=errors,
		)

	@staticmethod
	def async_get_options_flow(config_entry):
		return ComponentUpdaterOptionsFlow(config_entry)


class ComponentUpdaterOptionsFlow(config_entries.OptionsFlow):
	def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
		self._components: list[dict] = list(config_entry.options.get(CONF_COMPONENTS, []))
		self._server_url: str = config_entry.data.get(CONF_SERVER_URL, "")
		self._token: str = config_entry.data.get(CONF_TOKEN, "")
		self._token_username: str = config_entry.data.get(CONF_TOKEN_USERNAME, "")

	async def async_step_init(self, user_input: dict[str, Any] | None = None):
		return self.async_show_menu(
			step_id="init",
			menu_options=["add_component", "remove_component"],
		)

	async def async_step_add_component(self, user_input: dict[str, Any] | None = None):
		errors: dict[str, str] = {}

		if user_input is not None:
			existing_names = [c[CONF_COMPONENT_NAME] for c in self._components]
			if user_input[CONF_COMPONENT_NAME] in existing_names:
				errors[CONF_COMPONENT_NAME] = "duplicate_component"
			else:
				error = await _test_project_access(
					self._server_url,
					self._token,
					self._token_username,
					user_input[CONF_PROJECT_PATH].strip(),
				)
				if error:
					errors["base"] = error
			if not errors:
				component_name = user_input[CONF_COMPONENT_NAME].strip()
				self._components.append({
					CONF_PROJECT_PATH: user_input[CONF_PROJECT_PATH].strip(),
					CONF_COMPONENT_NAME: component_name,
					CONF_MODE: user_input[CONF_MODE],
					CONF_REF: user_input[CONF_REF].strip(),
					CONF_ARCHIVE_SUBDIR: user_input.get(CONF_ARCHIVE_SUBDIR, "").strip(),
					CONF_DEST_TYPE: user_input.get(CONF_DEST_TYPE, DEST_TYPE_CUSTOM_COMPONENT),
					CONF_AUTO_UPDATE: user_input.get(CONF_AUTO_UPDATE, False),
				})
				# Auto-install once async_create_entry below has reloaded the entry and
				# the component is registered. Polling (see _install_when_ready) replaces
				# the old fixed 5s timer, which raced the reload and failed with "not
				# found in any configured entry".
				self.hass.async_create_task(
					_install_when_ready(self.hass, component_name),
					name=f"deployer_autoinstall_{component_name}",
				)
				return self.async_create_entry(title="", data={CONF_COMPONENTS: self._components})

		return self.async_show_form(
			step_id="add_component",
			data_schema=_component_schema(),
			errors=errors,
			description_placeholders={
				"archive_subdir_hint": "e.g. custom_components/energy_manager (leave empty if component is at repo root)"
			},
		)

	async def async_step_remove_component(self, user_input: dict[str, Any] | None = None):
		if not self._components:
			return self.async_abort(reason="no_components")

		if user_input is not None:
			name = user_input.get("component")
			self._components = [c for c in self._components if c[CONF_COMPONENT_NAME] != name]
			return self.async_create_entry(
				title="",
				data={CONF_COMPONENTS: self._components},
			)

		return self.async_show_form(
			step_id="remove_component",
			data_schema=vol.Schema({
				vol.Required("component"): vol.In(
					{c[CONF_COMPONENT_NAME]: c[CONF_COMPONENT_NAME] for c in self._components}
				),
			}),
		)

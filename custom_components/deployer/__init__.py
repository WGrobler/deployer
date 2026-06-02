from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import CONF_COMPONENTS, DOMAIN
from .coordinator import DeployerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	coordinator = DeployerCoordinator(hass, entry)
	await coordinator.async_config_entry_first_refresh()

	hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

	await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
	entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

	_register_services(hass, coordinator)

	return True


def _register_services(hass: HomeAssistant, coordinator: DeployerCoordinator) -> None:
	# Only register once (first config entry wins; additional entries share the same services)
	if hass.services.has_service(DOMAIN, "install"):
		return

	async def handle_install(call: ServiceCall) -> None:
		component_name: str = call.data["component_name"]
		for coord in hass.data[DOMAIN].values():
			names = [c["component_name"] for c in coord.components]
			if component_name in names:
				await coord.async_install_component(component_name)
				return
		raise ValueError(f"Component '{component_name}' not found in any configured entry")

	async def handle_check_updates(call: ServiceCall) -> None:
		for coord in hass.data[DOMAIN].values():
			await coord.async_refresh()

	async def handle_update_all(call: ServiceCall) -> None:
		updated: list[str] = []
		for coord in hass.data[DOMAIN].values():
			updated.extend(await coord.async_update_all())
		if updated:
			_LOGGER.info("Auto-updated components: %s", ", ".join(updated))

	async def handle_restart_ha(call: ServiceCall) -> None:
		await hass.services.async_call("homeassistant", "restart")

	async def handle_add_component(call: ServiceCall) -> None:
		from .const import (
			CONF_ARCHIVE_SUBDIR, CONF_AUTO_UPDATE, CONF_COMPONENT_NAME,
			CONF_COMPONENTS, CONF_MODE, CONF_PROJECT_PATH, CONF_REF,
		)
		from .installer import _run, build_clone_url

		# Use the first (and typically only) config entry
		coord = next(iter(hass.data[DOMAIN].values()))
		entry = coord.entry

		component_name: str = call.data["component_name"]
		existing = [c[CONF_COMPONENT_NAME] for c in coord.components]
		if component_name in existing:
			raise ValueError(f"Component '{component_name}' is already configured")

		project_path: str = call.data["project_path"]
		ref: str = call.data.get("ref", "main")

		# Validate access before saving
		clone_url = build_clone_url(coord.server_url, coord.token, coord.token_username, project_path)
		rc, stdout, stderr = await _run(["git", "ls-remote", clone_url, "HEAD"], timeout=15)
		if rc != 0:
			raise ValueError(f"Cannot access repository '{project_path}': {stderr.strip()}")

		new_comp = {
			CONF_PROJECT_PATH: project_path,
			CONF_COMPONENT_NAME: component_name,
			CONF_MODE: call.data.get("mode", "branch"),
			CONF_REF: ref,
			CONF_ARCHIVE_SUBDIR: call.data.get("archive_subdir", ""),
			CONF_AUTO_UPDATE: call.data.get("auto_update", False),
		}
		updated_components = list(coord.components) + [new_comp]
		hass.config_entries.async_update_entry(
			entry, options={CONF_COMPONENTS: updated_components}
		)
		_LOGGER.info("Added component %s to deployer", component_name)

	async def handle_remove_component(call: ServiceCall) -> None:
		from .const import CONF_COMPONENT_NAME, CONF_COMPONENTS
		coord = next(iter(hass.data[DOMAIN].values()))
		entry = coord.entry
		component_name: str = call.data["component_name"]
		updated = [c for c in coord.components if c[CONF_COMPONENT_NAME] != component_name]
		if len(updated) == len(coord.components):
			raise ValueError(f"Component '{component_name}' not found")
		hass.config_entries.async_update_entry(entry, options={CONF_COMPONENTS: updated})
		_LOGGER.info("Removed component %s from deployer", component_name)

	hass.services.async_register(
		DOMAIN, "install", handle_install,
		schema=vol.Schema({vol.Required("component_name"): cv.string}),
	)
	hass.services.async_register(DOMAIN, "check_updates", handle_check_updates)
	hass.services.async_register(DOMAIN, "update_all", handle_update_all)
	hass.services.async_register(DOMAIN, "restart_ha", handle_restart_ha)
	hass.services.async_register(
		DOMAIN, "add_component", handle_add_component,
		schema=vol.Schema({
			vol.Required("project_path"): cv.string,
			vol.Required("component_name"): cv.string,
			vol.Optional("mode", default="branch"): vol.In(["branch", "tag"]),
			vol.Optional("ref", default="main"): cv.string,
			vol.Optional("archive_subdir", default=""): cv.string,
			vol.Optional("auto_update", default=False): cv.boolean,
		}),
	)
	hass.services.async_register(
		DOMAIN, "remove_component", handle_remove_component,
		schema=vol.Schema({vol.Required("component_name"): cv.string}),
	)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
	if unload_ok:
		hass.data[DOMAIN].pop(entry.entry_id)

	# Remove services when last entry is unloaded
	if not hass.data.get(DOMAIN):
		for service in ("install", "check_updates", "update_all", "restart_ha"):
			hass.services.async_remove(DOMAIN, service)

	return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
	await hass.config_entries.async_reload(entry.entry_id)

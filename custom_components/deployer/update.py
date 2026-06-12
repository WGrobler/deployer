from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COMPONENT_NAME, CONF_COMPONENTS, DOMAIN, MODE_BRANCH
from .coordinator import DeployerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry,
	async_add_entities: AddEntitiesCallback,
) -> None:
	coordinator: DeployerCoordinator = hass.data[DOMAIN][entry.entry_id]
	components = entry.options.get(CONF_COMPONENTS, [])
	current = {comp[CONF_COMPONENT_NAME] for comp in components}

	# Removing a component just stops re-creating its entity; the old registry entry
	# would otherwise linger as an unavailable orphan. Prune entries no longer backed
	# by a configured component before adding the current ones.
	_prune_stale_registrations(hass, entry, current)

	async_add_entities(
		DeployerUpdateEntity(coordinator, name) for name in current
	)


@callback
def _prune_stale_registrations(hass: HomeAssistant, entry: ConfigEntry, current: set[str]) -> None:
	"""Remove update entities/devices for components no longer in this entry."""
	ent_reg = er.async_get(hass)
	prefix = f"{entry.entry_id}_"
	for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
		if ent.domain != "update" or not (ent.unique_id or "").startswith(prefix):
			continue
		if ent.unique_id[len(prefix):] not in current:
			ent_reg.async_remove(ent.entity_id)
			_LOGGER.info("Deployer: removed stale update entity %s", ent.entity_id)

	dev_reg = dr.async_get(hass)
	for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
		comp_names = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
		# Device identifier is (DOMAIN, component_name); unlink this entry from devices
		# whose components are all gone (HA deletes the device once no entries remain).
		if comp_names and comp_names.isdisjoint(current):
			dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
			_LOGGER.info("Deployer: removed stale device for %s", ", ".join(sorted(comp_names)))


class DeployerUpdateEntity(CoordinatorEntity, UpdateEntity):
	_attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
	_attr_has_entity_name = True
	_attr_name = None  # Uses device name as entity name

	def __init__(self, coordinator: DeployerCoordinator, component_name: str) -> None:
		super().__init__(coordinator)
		self._component_name = component_name
		self._attr_unique_id = f"{coordinator.entry.entry_id}_{component_name}"
		self._attr_title = component_name
		self._installing = False

	@property
	def _data(self) -> dict:
		if self.coordinator.data:
			return self.coordinator.data.get(self._component_name, {})
		return {}

	@property
	def device_info(self) -> DeviceInfo:
		return DeviceInfo(
			identifiers={(DOMAIN, self._component_name)},
			name=self._component_name,
			manufacturer="Deployer",
			model=self._data.get("project_path", ""),
			sw_version=self.installed_version,
		)

	@property
	def installed_version(self) -> str | None:
		d = self._data
		if d.get("mode") == MODE_BRANCH:
			commit = d.get("installed_commit")
			# Return "not_installed" rather than None so HA state resolves to ON
			# (None causes state=unknown which hides the Install button in 2026.x)
			return commit[:7] if commit else "not_installed"
		return d.get("installed_ref") or "not_installed"

	@property
	def latest_version(self) -> str | None:
		d = self._data
		if d.get("mode") == MODE_BRANCH:
			latest = d.get("latest_commit")
			installed = d.get("installed_commit")
			if latest:
				return latest[:7]
			# Can't reach remote — show installed to avoid false update indicator
			return installed[:7] if installed else None
		# Tag mode: configured tag is the target version
		return d.get("configured_ref") or None

	@property
	def in_progress(self) -> bool:
		return self._installing

	@property
	def release_summary(self) -> str | None:
		d = self._data
		mode = d.get("mode", "")
		ref = d.get("configured_ref", "")
		last = d.get("last_installed", "")
		lines = [f"Tracking **{mode}** `{ref}`"]
		if last:
			lines.append(f"Last installed: {last[:19].replace('T', ' ')} UTC")
		return "\n".join(lines)

	async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
		self._installing = True
		self.async_write_ha_state()
		try:
			await self.coordinator.async_install_component(self._component_name)
		finally:
			self._installing = False
			self.async_write_ha_state()

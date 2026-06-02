from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COMPONENT_NAME, CONF_COMPONENTS, DOMAIN
from .coordinator import DeployerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry,
	async_add_entities: AddEntitiesCallback,
) -> None:
	coordinator: DeployerCoordinator = hass.data[DOMAIN][entry.entry_id]

	entities = [
		DeployerSensor(coordinator, comp[CONF_COMPONENT_NAME])
		for comp in entry.options.get(CONF_COMPONENTS, [])
	]
	async_add_entities(entities)


class DeployerSensor(CoordinatorEntity, SensorEntity):
	def __init__(self, coordinator: DeployerCoordinator, component_name: str) -> None:
		super().__init__(coordinator)
		self._component_name = component_name
		self._attr_unique_id = f"{coordinator.entry.entry_id}_{component_name}"
		self._attr_name = f"Deployer: {component_name}"

	@property
	def _data(self) -> dict:
		if self.coordinator.data:
			return self.coordinator.data.get(self._component_name, {})
		return {}

	@property
	def native_value(self) -> str | None:
		return self._data.get("state")

	@property
	def icon(self) -> str:
		state = self._data.get("state")
		if state == "update_available":
			return "mdi:arrow-up-circle"
		if state == "up_to_date":
			return "mdi:check-circle"
		if state == "not_installed":
			return "mdi:download-circle-outline"
		return "mdi:help-circle"

	@property
	def extra_state_attributes(self) -> dict:
		d = self._data
		return {
			"mode": d.get("mode"),
			"configured_ref": d.get("configured_ref"),
			"installed_ref": d.get("installed_ref"),
			"installed_commit": d.get("installed_commit"),
			"latest_commit": d.get("latest_commit"),
			"update_available": d.get("update_available"),
			"last_installed": d.get("last_installed"),
			"project_path": d.get("project_path"),
			"error": d.get("error"),
		}

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
	CONF_AUTO_UPDATE,
	CONF_COMPONENT_NAME,
	CONF_COMPONENTS,
	CONF_SERVER_URL,
	CONF_MODE,
	CONF_REF,
	CONF_TOKEN,
	CONF_TOKEN_USERNAME,
	DEFAULT_SCAN_INTERVAL,
	DOMAIN,
	MODE_BRANCH,
	STATE_NOT_INSTALLED,
	STATE_UNKNOWN,
	STATE_UP_TO_DATE,
	STATE_UPDATE_AVAILABLE,
)
from .installer import get_installed_meta, get_latest_commit, install_component

_LOGGER = logging.getLogger(__name__)


class DeployerCoordinator(DataUpdateCoordinator):
	def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
		super().__init__(
			hass,
			_LOGGER,
			name=DOMAIN,
			update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
		)
		self.entry = entry
		self.server_url: str = entry.data[CONF_SERVER_URL]
		self.token: str = entry.data.get(CONF_TOKEN, "")
		self.token_username: str = entry.data.get(CONF_TOKEN_USERNAME, "")

	@property
	def components(self) -> list[dict]:
		return self.entry.options.get(CONF_COMPONENTS, [])

	async def _async_update_data(self) -> dict:
		result: dict = {}

		for comp in self.components:
			name = comp[CONF_COMPONENT_NAME]
			try:
				meta = await self.hass.async_add_executor_job(get_installed_meta, self.hass, name)
				latest_commit: str | None = None

				if comp[CONF_MODE] == MODE_BRANCH:
					try:
						latest_commit = await get_latest_commit(
							self.server_url, self.token, self.token_username,
							comp["project_path"], comp[CONF_REF],
						)
					except Exception as err:
						_LOGGER.warning("Could not fetch latest commit for %s: %s", name, err)

					if meta:
						update_available = (
							latest_commit is not None
							and meta.get("installed_commit") != latest_commit
						)
						state = STATE_UPDATE_AVAILABLE if update_available else STATE_UP_TO_DATE
					else:
						state = STATE_NOT_INSTALLED
						update_available = True
				else:
					if meta:
						update_available = meta.get("installed_ref") != comp[CONF_REF]
						state = STATE_UPDATE_AVAILABLE if update_available else STATE_UP_TO_DATE
					else:
						state = STATE_NOT_INSTALLED
						update_available = True

				result[name] = {
					"state": state,
					"mode": comp[CONF_MODE],
					"configured_ref": comp[CONF_REF],
					"installed_ref": meta.get("installed_ref") if meta else None,
					"installed_commit": meta.get("installed_commit") if meta else None,
					"latest_commit": latest_commit,
					"update_available": update_available,
					"last_installed": meta.get("installed_at") if meta else None,
					"project_path": comp.get("project_path"),
				}

			except Exception as err:
				_LOGGER.error("Error checking component %s: %s", name, err)
				result[name] = {
					"state": STATE_UNKNOWN,
					"update_available": False,
					"error": str(err),
				}

		return result

	async def async_install_component(self, component_name: str) -> None:
		comp = next(
			(c for c in self.components if c[CONF_COMPONENT_NAME] == component_name),
			None,
		)
		if not comp:
			raise ValueError(f"Component '{component_name}' is not configured in this entry")

		await install_component(
			self.hass, self.server_url, self.token, self.token_username, comp
		)
		await self.async_refresh()

	async def async_update_all(self) -> list[str]:
		updated: list[str] = []
		for comp in self.components:
			if not comp.get(CONF_AUTO_UPDATE):
				continue
			name = comp[CONF_COMPONENT_NAME]
			if self.data and self.data.get(name, {}).get("update_available"):
				try:
					await self.async_install_component(name)
					updated.append(name)
				except Exception as err:
					_LOGGER.error("Auto-update failed for %s: %s", name, err)
		return updated

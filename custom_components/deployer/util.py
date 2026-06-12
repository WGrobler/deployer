"""Pure helpers for the deployer integration.

This module intentionally avoids importing Home Assistant so the credential and
URL helpers can be unit-tested without a running HA instance.
"""

from __future__ import annotations

import logging
import re

_LOGGER = logging.getLogger(__name__)

# Matches the credentials portion of an authenticated URL, e.g.
# "https://user:token@host" or "https://oauth2:token@host".
_CREDS_RE = re.compile(r"(https?://)[^/@\s]+@")


def redact_url(text: str | None) -> str | None:
	"""Strip embedded credentials from any string before it is logged.

	build_clone_url() embeds the deploy token in the clone URL. Git echoes that
	URL back in its stderr, and several error paths interpolate the URL or git's
	output into log messages — which would leak the token. Replace the
	"user:token@" segment of any URL with "***@".
	"""
	if not text:
		return text
	return _CREDS_RE.sub(r"\1***@", text)


def build_clone_url(server_url: str, token: str, token_username: str, project_path: str) -> str:
	"""Build an authenticated HTTPS clone URL."""
	host = server_url.replace("https://", "").replace("http://", "")
	scheme = "https" if server_url.startswith("https") else "http"
	if token and token_username:
		return f"{scheme}://{token_username}:{token}@{host}/{project_path}.git"
	if token:
		return f"{scheme}://oauth2:{token}@{host}/{project_path}.git"
	return f"{scheme}://{host}/{project_path}.git"


async def install_in_background(hass, server_url: str, token: str, token_username: str, comp: dict) -> None:
	"""Install a freshly-added component without racing the options reload.

	Adding a component persists the options and asynchronously reloads the config
	entry. The previous implementation then either fired a fixed 5s timer or polled
	for the component to appear in a coordinator before calling deployer.install —
	both of which depend on the reload's timing and produced the "not found in any
	configured entry" failure when they lost the race.

	Because install_component() takes the credentials and component dict directly,
	we can install without any coordinator lookup, so the reload timing is
	irrelevant. A coordinator refresh afterwards updates the matching update entity.
	"""
	# Local imports keep this module's top level free of HA/const dependencies so the
	# pure helpers above can be unit-tested standalone; the installer import also
	# avoids a circular import (installer imports from this module).
	from .const import CONF_COMPONENT_NAME, DOMAIN
	from .installer import install_component

	name = comp[CONF_COMPONENT_NAME]
	try:
		await install_component(hass, server_url, token, token_username, comp)
	except Exception as err:  # noqa: BLE001 — surface install failures instead of leaking
		_LOGGER.error("Deployer: auto-install of %s failed: %s", name, redact_url(str(err)))
		return

	# Refresh the coordinator that now owns this component so its update entity
	# reflects the freshly-installed version instead of waiting for the next scan.
	for coord in hass.data.get(DOMAIN, {}).values():
		if any(c[CONF_COMPONENT_NAME] == name for c in coord.components):
			await coord.async_request_refresh()
			return

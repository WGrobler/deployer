from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


from homeassistant.core import HomeAssistant

from .const import (
	CONF_ARCHIVE_SUBDIR,
	CONF_COMPONENT_NAME,
	CONF_DEST_TYPE,
	CONF_PROJECT_PATH,
	CONF_REF,
	DEST_TYPE_WWW,
	DEST_TYPE_CUSTOM_COMPONENT,
)

_LOGGER = logging.getLogger(__name__)

META_FILENAME = ".deployer_meta.json"


def _custom_components_path(hass: HomeAssistant) -> Path:
	return Path(hass.config.config_dir) / "custom_components"


def _www_path(hass: HomeAssistant) -> Path:
	return Path(hass.config.config_dir) / "www"


def _dest_root(hass: HomeAssistant, dest_type: str) -> Path:
	if dest_type == DEST_TYPE_WWW:
		return _www_path(hass)
	return _custom_components_path(hass)


def get_installed_meta(hass: HomeAssistant, component_name: str) -> dict | None:
	# Check custom_components first, then www (handles both dest types)
	for root in (_custom_components_path(hass), _www_path(hass)):
		meta_path = root / component_name / META_FILENAME
		if meta_path.exists():
			try:
				return json.loads(meta_path.read_text())
			except Exception:
				return None
	return None


def _save_meta(hass: HomeAssistant, component_name: str, meta: dict, dest_type: str) -> None:
	dest = _dest_root(hass, dest_type) / component_name
	dest.mkdir(parents=True, exist_ok=True)
	(dest / META_FILENAME).write_text(json.dumps(meta, indent=2))


def build_clone_url(server_url: str, token: str, token_username: str, project_path: str) -> str:
	"""Build an authenticated HTTPS clone URL."""
	host = server_url.replace("https://", "").replace("http://", "")
	scheme = "https" if server_url.startswith("https") else "http"
	if token and token_username:
		return f"{scheme}://{token_username}:{token}@{host}/{project_path}.git"
	if token:
		return f"{scheme}://oauth2:{token}@{host}/{project_path}.git"
	return f"{scheme}://{host}/{project_path}.git"


async def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
	import os
	env = os.environ.copy()
	# Prevent git from prompting for credentials or opening any terminal UI
	env["GIT_TERMINAL_PROMPT"] = "0"
	env["GIT_ASKPASS"] = "echo"
	env["SSH_ASKPASS"] = "echo"
	proc = await asyncio.create_subprocess_exec(
		*cmd,
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE,
		env=env,
	)
	try:
		stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
	except asyncio.TimeoutError:
		proc.kill()
		await proc.communicate()
		raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd[:3])}")
	return proc.returncode, stdout.decode(), stderr.decode()


async def get_latest_commit(
	server_url: str,
	token: str,
	token_username: str,
	project_path: str,
	ref: str,
) -> str | None:
	"""Return the latest commit SHA for a ref using git ls-remote (no API needed)."""
	clone_url = build_clone_url(server_url, token, token_username, project_path)
	rc, stdout, stderr = await _run(["git", "ls-remote", clone_url, f"refs/heads/{ref}", f"refs/tags/{ref}"])
	if rc != 0:
		raise RuntimeError(f"git ls-remote failed: {stderr.strip()}")
	for line in stdout.splitlines():
		sha, _ = line.split("\t", 1)
		return sha.strip()
	return None


def _copy_component(source: Path, dest_path: Path) -> None:
	"""Blocking file copy — must be called in an executor."""
	import os
	if not source.exists():
		raise RuntimeError(
			f"Path not found in repo. "
			f"Repo root contains: {os.listdir(source.parent)}"
		)
	if dest_path.exists():
		shutil.rmtree(dest_path)
	shutil.copytree(str(source), str(dest_path))


async def _register_lovelace_resources(
	hass: HomeAssistant, component_name: str, dest_path: Path, commit_sha: str | None = None
) -> list[str]:
	"""Register or update top-level .js files as Lovelace module resources.

	Existing resources matching the base URL are updated with a new ?v= tag so
	browsers pick up the new file without a manual cache clear.
	"""
	js_files = await hass.async_add_executor_job(lambda: sorted(dest_path.glob("*.js")))
	if not js_files:
		_LOGGER.warning("Deployer: no .js files found in %s — skipping Lovelace resource registration", dest_path)
		return []

	# hass.data["lovelace"] is a LovelaceData object (not a dict) — use attribute access
	lovelace_data = hass.data.get("lovelace")
	resources = getattr(lovelace_data, "resources", None)
	if resources is None:
		_LOGGER.warning("Deployer: lovelace resources collection not available — register resources manually")
		return []

	version_suffix = f"?v={commit_sha[:7]}" if commit_sha else ""
	# Map base URL (without query string) → resource id for existing entries
	existing_by_base = {item["url"].split("?")[0]: item["id"] for item in resources.async_items()}

	result: list[str] = []
	for js_file in js_files:
		base_url = f"/local/{component_name}/{js_file.name}"
		versioned_url = f"{base_url}{version_suffix}"
		if base_url in existing_by_base:
			await resources.async_update_item(existing_by_base[base_url], {"url": versioned_url})
			_LOGGER.info("Deployer: updated Lovelace resource %s", versioned_url)
		else:
			await resources.async_create_item({"type": "module", "url": versioned_url})
			_LOGGER.info("Deployer: registered Lovelace resource %s", versioned_url)
		result.append(versioned_url)

	return result


async def install_component(
	hass: HomeAssistant,
	server_url: str,
	token: str,
	token_username: str,
	comp: dict,
) -> None:
	component_name = comp[CONF_COMPONENT_NAME]
	project_path = comp[CONF_PROJECT_PATH]
	ref = comp[CONF_REF]
	archive_subdir = comp.get(CONF_ARCHIVE_SUBDIR, "").strip().lstrip("/")
	dest_type = comp.get(CONF_DEST_TYPE, DEST_TYPE_CUSTOM_COMPONENT)

	clone_url = build_clone_url(server_url, token, token_username, project_path)
	dest_path = _dest_root(hass, dest_type) / component_name

	_LOGGER.info("Cloning %s @ %s (dest_type=%s)", project_path, ref, dest_type)

	tmpdir_obj = tempfile.TemporaryDirectory()
	tmpdir = tmpdir_obj.name
	try:
		rc, stdout, stderr = await _run(
			["git", "clone", "--depth", "1", "--branch", ref, clone_url, tmpdir],
			timeout=120,
		)
		if rc != 0:
			raise RuntimeError(f"git clone failed: {stderr.strip()}")

		rc2, sha_out, _ = await _run(["git", "-C", tmpdir, "rev-parse", "HEAD"])
		commit_sha = sha_out.strip() if rc2 == 0 else None

		source = Path(tmpdir)
		if archive_subdir:
			source = source / archive_subdir

		await hass.async_add_executor_job(_copy_component, source, dest_path)
	finally:
		await hass.async_add_executor_job(tmpdir_obj.cleanup)

	meta = {
		"installed_ref": ref,
		"installed_commit": commit_sha,
		"project_path": project_path,
		"dest_type": dest_type,
		"installed_at": datetime.now(timezone.utc).isoformat(),
	}
	await hass.async_add_executor_job(_save_meta, hass, component_name, meta, dest_type)
	_LOGGER.info("Installed %s (%s @ %s)", component_name, commit_sha or "?", ref)

	short_sha = commit_sha[:7] if commit_sha else ref

	if dest_type == DEST_TYPE_WWW:
		registered = await _register_lovelace_resources(hass, component_name, dest_path, commit_sha)
		resource_lines = "\n".join(f"- `{url}`" for url in registered) if registered else "_No new resources registered._"
		await hass.services.async_call(
			"persistent_notification",
			"create",
			{
				"title": f"Deployer: {component_name} Installed",
				"message": (
					f"**{component_name}** ({short_sha}) has been installed to `/config/www/{component_name}/`.\n\n"
					f"Lovelace resources registered:\n{resource_lines}\n\n"
					"Refresh your browser to load the updated card."
				),
				"notification_id": f"deployer_installed_{component_name}",
			},
		)
	else:
		await hass.services.async_call(
			"persistent_notification",
			"create",
			{
				"title": "Deployer: Restart Required",
				"message": (
					f"**{component_name}** ({short_sha}) has been installed.\n\n"
					"Restart Home Assistant to activate the changes."
				),
				"notification_id": f"deployer_restart_{component_name}",
			},
		)

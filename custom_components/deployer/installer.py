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
from .util import build_clone_url, redact_url

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
		raise RuntimeError(f"Command timed out after {timeout}s: {redact_url(' '.join(cmd[:3]))}")
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
		raise RuntimeError(f"git ls-remote failed: {redact_url(stderr.strip())}")
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
		_LOGGER.error(
			"Deployer: Lovelace resources collection unavailable — register the %s resources "
			"manually under Settings → Dashboards → Resources", component_name,
		)
		return []

	# YAML-mode resources are read-only; they cannot be registered programmatically.
	if not hasattr(resources, "async_create_item"):
		_LOGGER.error(
			"Deployer: Lovelace is in YAML resource mode — add the %s resources to your "
			"lovelace resources YAML manually", component_name,
		)
		return []

	# A storage-backed ResourceStorageCollection must be loaded before its items can be
	# listed or created. Right after a config-entry reload (e.g. the auto-install that
	# follows add_component) it often isn't loaded yet, so async_items()/async_create_item
	# would silently no-op. Force a load first.
	if getattr(resources, "loaded", True) is False:
		await resources.async_load()
		resources.loaded = True

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
			await resources.async_create_item({"res_type": "module", "url": versioned_url})
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
			raise RuntimeError(f"git clone failed: {redact_url(stderr.strip())}")

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
		if registered:
			resource_lines = "\n".join(f"- `{url}`" for url in registered)
			title = f"Deployer: {component_name} Installed"
			message = (
				f"**{component_name}** ({short_sha}) has been installed to `/config/www/{component_name}/`.\n\n"
				f"Lovelace resources registered:\n{resource_lines}\n\n"
				"Refresh your browser to load the updated card."
			)
		else:
			# Files copied but no resource registered (YAML mode, collection not ready,
			# or no .js found). Make the failure actionable instead of silent — the
			# deployer re-registers www resources on every start (see async_setup_entry).
			title = f"Deployer: {component_name} Installed — action needed"
			message = (
				f"**{component_name}** ({short_sha}) was installed to `/config/www/{component_name}/`, "
				"but no Lovelace resource could be registered automatically.\n\n"
				"Restart Home Assistant (the deployer re-registers www resources on start) or add "
				f"`/local/{component_name}/<file>.js` manually under Settings → Dashboards → Resources."
			)
		await hass.services.async_call(
			"persistent_notification",
			"create",
			{
				"title": title,
				"message": message,
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


async def async_register_installed_www_resources(hass: HomeAssistant, components: list[dict]) -> None:
	"""Re-register Lovelace resources for every installed www component.

	Resource registration at install time can silently no-op (the storage-backed
	ResourceStorageCollection may not be loaded yet right after a config-entry
	reload), which leaves cards on disk but unusable. Running this on every
	async_setup_entry makes registration self-healing: a restart re-registers any
	missing resources. It is idempotent — _register_lovelace_resources updates
	existing entries in place rather than duplicating them.
	"""
	for comp in components:
		if comp.get(CONF_DEST_TYPE) != DEST_TYPE_WWW:
			continue
		name = comp[CONF_COMPONENT_NAME]
		dest_path = _www_path(hass) / name
		if not await hass.async_add_executor_job(dest_path.exists):
			continue
		meta = await hass.async_add_executor_job(get_installed_meta, hass, name)
		commit_sha = meta.get("installed_commit") if meta else None
		try:
			await _register_lovelace_resources(hass, name, dest_path, commit_sha)
		except Exception as err:  # noqa: BLE001 — best-effort self-heal, never block setup
			_LOGGER.warning("Deployer: could not ensure Lovelace resources for %s: %s", name, err)


async def _unregister_lovelace_resources(hass: HomeAssistant, component_name: str) -> None:
	"""Remove any Lovelace resources pointing at /local/<component_name>/."""
	lovelace_data = hass.data.get("lovelace")
	resources = getattr(lovelace_data, "resources", None)
	if resources is None or not hasattr(resources, "async_delete_item"):
		return
	if getattr(resources, "loaded", True) is False:
		await resources.async_load()
		resources.loaded = True
	prefix = f"/local/{component_name}/"
	for item in list(resources.async_items()):
		if item["url"].split("?")[0].startswith(prefix):
			await resources.async_delete_item(item["id"])
			_LOGGER.info("Deployer: removed Lovelace resource %s", item["url"])


async def async_uninstall_component(hass: HomeAssistant, component_name: str, dest_type: str) -> None:
	"""Delete a component's installed files and, for www, its Lovelace resources."""
	if dest_type == DEST_TYPE_WWW:
		await _unregister_lovelace_resources(hass, component_name)

	dest_path = _dest_root(hass, dest_type) / component_name
	config_root = Path(hass.config.config_dir).resolve()

	def _rm() -> None:
		target = dest_path.resolve()
		# Safety: only ever delete a named sub-directory inside the HA config dir,
		# never a dest root itself or anything outside config_dir.
		if not component_name or target.name != component_name:
			return
		if target == config_root or config_root not in target.parents:
			_LOGGER.error("Deployer: refusing to delete unsafe path %s", target)
			return
		if target.exists():
			shutil.rmtree(target)
			_LOGGER.info("Deployer: deleted %s", target)

	await hass.async_add_executor_job(_rm)

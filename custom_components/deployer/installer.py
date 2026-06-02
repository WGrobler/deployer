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

from .const import CONF_ARCHIVE_SUBDIR, CONF_COMPONENT_NAME, CONF_PROJECT_PATH, CONF_REF

_LOGGER = logging.getLogger(__name__)

META_FILENAME = ".deployer_meta.json"


def _custom_components_path(hass: HomeAssistant) -> Path:
	return Path(hass.config.config_dir) / "custom_components"


def get_installed_meta(hass: HomeAssistant, component_name: str) -> dict | None:
	meta_path = _custom_components_path(hass) / component_name / META_FILENAME
	if not meta_path.exists():
		return None
	try:
		return json.loads(meta_path.read_text())
	except Exception:
		return None


def _save_meta(hass: HomeAssistant, component_name: str, meta: dict) -> None:
	dest = _custom_components_path(hass) / component_name
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
	archive_subdir = comp.get(CONF_ARCHIVE_SUBDIR, "")

	clone_url = build_clone_url(server_url, token, token_username, project_path)
	dest_path = _custom_components_path(hass) / component_name

	_LOGGER.info("Cloning %s @ %s", project_path, ref)

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
		"installed_at": datetime.now(timezone.utc).isoformat(),
	}
	await hass.async_add_executor_job(_save_meta, hass, component_name, meta)
	_LOGGER.info("Installed %s (%s @ %s)", component_name, commit_sha or "?", ref)

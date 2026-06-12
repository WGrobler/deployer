"""Unit tests for the deployer's pure helpers.

util.py keeps its module level free of Home Assistant / package imports, so it
can be loaded directly here without a running HA instance.
"""

import importlib.util
from pathlib import Path

_UTIL_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "deployer" / "util.py"
_spec = importlib.util.spec_from_file_location("deployer_util", _UTIL_PATH)
util = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(util)


# --- redact_url -----------------------------------------------------------

def test_redact_url_strips_userinfo_credentials():
	url = "https://oauth2:abc123secret@gitlab.com/group/project.git"
	assert util.redact_url(url) == "https://***@gitlab.com/group/project.git"


def test_redact_url_strips_user_and_token():
	url = "https://deploy-user:tok-xyz@gitlab.com/g/p.git"
	assert util.redact_url(url) == "https://***@gitlab.com/g/p.git"


def test_redact_url_redacts_inside_a_longer_message():
	msg = "git clone failed: fatal: could not read from https://u:t@host/x.git"
	assert "u:t@" not in util.redact_url(msg)
	assert "https://***@host/x.git" in util.redact_url(msg)


def test_redact_url_leaves_unauthenticated_urls_untouched():
	url = "https://gitlab.com/group/project.git"
	assert util.redact_url(url) == url


def test_redact_url_passes_through_empty_and_none():
	assert util.redact_url("") == ""
	assert util.redact_url(None) is None


# --- build_clone_url ------------------------------------------------------

def test_build_clone_url_token_with_username():
	assert (
		util.build_clone_url("https://gitlab.com", "TOK", "deploy", "grp/proj")
		== "https://deploy:TOK@gitlab.com/grp/proj.git"
	)


def test_build_clone_url_token_without_username_uses_oauth2():
	assert (
		util.build_clone_url("https://gitlab.com", "TOK", "", "grp/proj")
		== "https://oauth2:TOK@gitlab.com/grp/proj.git"
	)


def test_build_clone_url_public_no_token():
	assert (
		util.build_clone_url("https://github.com", "", "", "owner/repo")
		== "https://github.com/owner/repo.git"
	)


def test_build_clone_url_http_scheme_preserved():
	assert util.build_clone_url("http://git.local", "", "", "g/p").startswith("http://")


def test_redacting_a_built_url_hides_the_token():
	url = util.build_clone_url("https://gitlab.com", "SECRET", "deploy", "g/p")
	assert "SECRET" not in util.redact_url(url)

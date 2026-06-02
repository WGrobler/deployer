# Deployer for Home Assistant

A Home Assistant custom integration that installs and updates other custom integrations directly from any **Git forge** — GitHub, GitLab, Gitea, or any self-hosted instance. Supports both public and private repositories.

Built for developers and integrators who distribute their own custom HA components and need a reliable, UI-controllable deployment mechanism without depending on HACS or GitHub accounts.

---

## Features

- Works with **GitHub, GitLab, Gitea**, and any self-hosted Git forge
- **Public repos** — no token required
- **Private repos** — supports Personal Access Tokens and Deploy Tokens (including group-level)
- Two tracking modes per component:
  - **Branch mode** — tracks the latest commit; checks for updates hourly via `git ls-remote`
  - **Tag mode** — pinned to a specific version; update on your schedule
- Status sensor per managed component (`up_to_date`, `update_available`, `not_installed`)
- All operations available as HA services — fully controllable from the UI, automations, or via the [Claude MCP server](https://github.com/homeassistant-ai/ha-mcp)
- No API rate limit concerns — uses native git operations, not REST APIs

---

## Installation

1. Copy the `custom_components/deployer` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Integrations → Add Integration** and search for **Deployer**.

> Deployer must be deployed once manually. After that it manages all your other integrations.

---

## Configuration

### Step 1 — Connect to your Git forge

A **Git forge** is any platform that hosts Git repositories (GitHub, GitLab, Gitea, etc.).

| Field | Description | Example |
|---|---|---|
| Connection Name | Label for this forge | `My GitLab` |
| Forge URL | Base URL | `https://gitlab.com` or `https://github.com` |
| Token Username | Deploy token username only — leave empty for PATs | `deploy-bot` |
| Access Token | PAT or deploy token — **leave empty for public repos** | `glpat-xxxx` |

**Token scopes required:**

| Forge | Token type | Required scope |
|---|---|---|
| GitLab | Personal Access Token | `read_api` |
| GitLab | Deploy Token (project or group) | `read_repository` |
| GitHub | Personal Access Token | `repo` (private) or none (public) |
| Any | Public repository | No token needed |

> GitLab group-level deploy tokens are fully supported. Deployer uses `git` operations rather than the REST API, so `read_repository` is sufficient.

### Step 2 — Add components

Open the integration options to add components, or use the `deployer.add_component` service.

| Field | Description | Example |
|---|---|---|
| Project Path | Namespace + project name only — no forge URL, no `.git` | `mygroup/my-integration` |
| Component Name | Folder name in `custom_components/` | `my_integration` |
| Mode | `branch` or `tag` | `branch` |
| Ref | Branch name or tag | `main` / `v1.2.0` |
| Archive Subdir | Path within the repo to the component files | `custom_components/my_integration` |
| Auto Update | Update automatically on new commits | off |

**Archive Subdir** — leave empty if the component files sit at the repo root. If they live in a subdirectory (common when a repo contains multiple things), set this to the relative path, e.g. `custom_components/my_integration`.

---

## Services

| Service | Description |
|---|---|
| `deployer.install` | Install or update a specific component |
| `deployer.add_component` | Add a new component to manage (validates access first) |
| `deployer.remove_component` | Remove a component from management |
| `deployer.check_updates` | Refresh update status for all components now |
| `deployer.update_all` | Update all components with auto-update enabled |
| `deployer.restart_ha` | Restart HA to activate newly installed components |

### Example: add and install via service call

```yaml
service: deployer.add_component
data:
  project_path: mygroup/my-integration
  component_name: my_integration
  mode: branch
  ref: main
  archive_subdir: custom_components/my_integration
  auto_update: false
```

```yaml
service: deployer.install
data:
  component_name: my_integration
```

---

## How it works

Deployer uses `git clone --depth 1` to download the repository at the configured branch or tag, then copies the component files into `/config/custom_components/`. A metadata file (`.deployer_meta.json`) is written alongside each component to track the installed version.

For branch mode, Deployer checks for updates once per hour using `git ls-remote` — a lightweight operation that fetches only the latest commit SHA without downloading any files. This uses native git protocol rather than forge REST APIs, so it does not count against GitHub or GitLab API rate limits.

**A HA restart is required after installing or updating a component** for the changes to take effect.

---

## Limitations

- Deployer itself cannot update itself (bootstrap problem) — update it manually or via your deployment pipeline.
- `git` must be available in the HA container (it is in the standard `homeassistant/home-assistant` Docker image).

---

## License

MIT — see [LICENSE](LICENSE).

---

## Contributing

Issues and PRs welcome at [github.com/WGrobler/deployer](https://github.com/WGrobler/deployer).

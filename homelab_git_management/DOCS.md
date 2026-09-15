# Homelab Git Management — Documentation

This is the documentation shown in this add-on's own **Documentation** tab
(Settings → Add-ons → Homelab Git Management → Documentation). See the
repository [`README.md`](../README.md) for the installation procedure.

## First start

1. Start the add-on once with no configuration.
2. Open its Web UI (Ingress). You'll see a setup screen with a generated
   SSH public key.
3. Add that key as a **read-only** Deploy Key on the GitHub repository you
   want to sync.
4. Fill in `github_repository` and `github_branch` in the Configuration
   tab, add at least one entry under `mappings`, and restart.

## Options reference

| Option | Type | Description |
|---|---|---|
| `github_repository` | string | `owner/repository`, e.g. `octocat/my-config`. Empty by default. |
| `github_branch` | string | Branch to track. `main` by default. |
| `mappings` | list | See below. Empty by default — nothing is managed until you add entries. |

Each entry under `mappings`:

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique, `[A-Za-z0-9_-]+` only. Used as the deploy target internally and in the API. |
| `kind` | `file` \| `directory` | Defaults to `file`. Directories can be compared but not deployed. |
| `ha_path` | string | Absolute path under `/config`. |
| `git_path` | string | Path relative to the repository root. |
| `direction` | `git_to_ha` \| `ha_to_git` \| `bidirectional` | Only `git_to_ha` is implemented; the other two are accepted by validation but make the add-on refuse to start, with an explicit error naming the offending mapping. |

## Managing mappings from the dashboard

Mappings can be created, edited and deleted directly from the Ingress
dashboard ("Configured mappings" panel), with a directory/file browser for
both the Home Assistant side and the Git side — no need to type raw paths
by hand. The native Configuration tab (Settings → Add-ons → Homelab Git
Management → Configuration) still works exactly as before; the dashboard
is an additional, optional way to manage the same list.

The directory browser only ever lists directories: it reads the Home
Assistant config mount and the local Git clone, never their content, and
never anything outside them. Dotfiles/dotdirs (`.storage`, `.git`, ...)
are hidden.

**Security note:** this is the one feature where the add-on writes its own
configuration, instead of only reading it. It does so through Home
Assistant Supervisor's own API, scoped to `/addons/self/options` — by
construction, this can only ever change this add-on's own configuration,
never another add-on's or Home Assistant's own files. Every mapping
submitted this way is re-validated with the exact same rules the engine
itself enforces (identifier format, allowed `kind`, implemented
`direction`, path containment, the four protected core files below)
before it is ever saved.

If a saved mapping isn't visible within about 1.5 seconds (Supervisor's
options write can lag before this add-on's own copy catches up), the
add-on restarts itself automatically to pick it up cleanly, the same
effect as a manual restart from the Info tab — the dashboard shows a
brief "restarting" overlay and recovers on its own. This uses the same
"self"-scoped Supervisor API (`/addons/self/restart`): it can only ever
restart this add-on, never another one or Home Assistant itself.

## Comparison states

- **identical** — byte-for-byte equal.
- **equivalent** — equal after normalizing UTF-8 BOM and line endings
  (still considered safe / no action needed).
- **different** — a real content difference. Eligible for deployment if
  `direction: git_to_ha`.
- **missing_ha** / **missing_git** / **missing_both** — the path doesn't
  exist on one or both sides.
- **error** — the path could not be checked (permissions, symlink, path
  escaping its root, etc.). Always treated as blocking.

## Protected core files

`configuration.yaml`, `scripts.yaml`, `automations.yaml` and `scenes.yaml`
under `/config` can never be deployed to by this add-on, even if you map
them with `direction: git_to_ha`. They can still be mapped for comparison.

## Deployment safety

Every deployment: backs up the current file (if any) to
`/data/deploy-backups/<id>/`, writes the new content to a temporary file in
the same directory, `fsync`s it, atomically renames it over the target,
re-reads it to verify it matches byte-for-byte, and re-classifies it to
confirm `identical`. If any of that fails, the original content (or
absence) is restored automatically before the add-on reports failure.

## Updating the add-on itself

This add-on never modifies its own source. Updates go through the normal
Supervisor flow: a new version is published in this repository, and you
click **Update** in Settings → Add-ons like for any other add-on.

## Logs

Every step (Git update, comparison, deployment, rollback) is logged with a
`[homelab-git-management]` prefix, visible in the add-on's **Log** tab. No
file content or secret is ever logged.

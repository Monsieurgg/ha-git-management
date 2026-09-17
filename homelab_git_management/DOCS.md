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
| `direction` | `git_to_ha` \| `ha_to_git` \| `bidirectional` | All three are implemented. |

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

## Pushing Home Assistant changes to Git (ha_to_git)

A mapping configured with `direction: ha_to_git` does the reverse of the
default direction: it can push the current Home Assistant file to your
GitHub repository, as a real commit, after explicit confirmation.

**A second, separate Deploy Key.** This needs write access, which the
original (read-only) Deploy Key deliberately never has. On first start,
the add-on generates a second, entirely independent SSH keypair for this,
shown in its own section of the Ingress UI ("Optional: write access").
Nothing changes about the original key, and no read-only code path
(comparison, `git_to_ha`) ever references the write-capable key. Until
you explicitly add this second public key on GitHub — this time allowing
write access — every `ha_to_git` mapping simply fails with a clear error.

**Conflict safety.** Because Git can now change from two places (a
`git_to_ha`/comparison read, and a direct edit on GitHub itself), the
add-on tracks, per `ha_to_git` mapping, what each side looked like the
last time they were confirmed in agreement
(`/data/sync-state.json`). This is what lets it tell apart "only Home
Assistant changed since" (safe to push) from "Git also changed
independently" (a real conflict) — a plain "most recent timestamp wins"
comparison cannot make that distinction, and could silently overwrite a
real edit made directly on GitHub. When a real conflict is detected, the
Push button is replaced with a **Resolve** button, which shows:

- when each side was last modified (Home Assistant's file mtime, Git's
  last commit date for that path);
- a line-by-line difference between the two versions;
- two resolutions: **force-push** the Home Assistant version (deliberately
  discarding the GitHub-side change), or **keep the Git version**
  (accepts GitHub's current content as the new reference point — nothing
  is pushed and Home Assistant is not touched; if the two still differ
  afterward, that shows up as a normal, safe-to-push difference again).

**Line-ending safety.** Before pushing, the add-on also checks whether the
Home Assistant file's line-ending convention (LF vs. CRLF) matches what is
already tracked in Git for that path. A mismatch — usually a sign that
something upstream (an editor, a network share, a text-mode file
transfer, ...) touched the live file — is blocked with a clear error
instead of being pushed: converting every line's ending would otherwise
bury the real, intended change in a diff touching the whole file.

**The one exception to "no file content is ever shown."** The Resolve
screen above is the single place in this add-on where real file content
reaches the browser, instead of only comparison metadata. It is scoped
narrowly: only for `ha_to_git` mappings in a blocked state, capped in
size, and skipped entirely (a "cannot preview" message instead) for
binary content. This content never leaves your own authenticated Ingress
session — see **Exposure and authentication** below.

## Two-way sync (bidirectional)

A mapping configured with `direction: bidirectional` combines both
directions above on the same file: either Deploy (Git → Home Assistant)
or Push (Home Assistant → Git) can apply, and the add-on decides which
button to show based on which side actually changed since the last
sync — never on raw content difference alone. Reusing the exact same
reference-based comparison built for `ha_to_git` (see above) is what
makes this safe: it is what tells apart "only Home Assistant changed"
from "only Git changed" from "both changed independently" — a plain
"latest edit wins" comparison cannot make that distinction and could
silently discard a real change on either side.

- Only Home Assistant changed → **Push** is offered.
- Only Git changed → **Deploy** is offered.
- Both changed since the last sync → neither button appears
  automatically. The mapping shows a **CONFLICT** badge and a
  **Resolve** button, exactly like `ha_to_git`'s conflict screen (dates
  + line-by-line diff), but with two resolutions that both actually
  write: **force-deploy** Git → Home Assistant, or **force-push** Home
  Assistant → Git. There is no passive "accept without syncing" choice
  here — for a one-way mapping that made sense (it just meant "stop
  offering to push"), but for a two-way mapping it would only leave the
  two sides mismatched and immediately show the same conflict again.
- A `bidirectional` mapping with no sync history yet and content that
  already differs on both sides also starts as a conflict: with two
  independent, unrelated histories and no prior agreement recorded,
  this add-on does not guess which one is authoritative — a human picks
  once, explicitly, via the same forced resolution above.
- The write-capable Deploy Key and the line-ending safety check
  described above for `ha_to_git` apply identically to a
  `bidirectional` mapping's Push side. Nothing additional to configure
  if `ha_to_git` is already set up.
- The four protected core files can be mapped with
  `direction: bidirectional`, but only their Push side is ever
  available — Deploy (writing them from Git) remains permanently
  blocked, exactly as for `git_to_ha`.

## Exposure and authentication

This add-on has no LAN port of its own (`ingress: true` with no `ports:`
entry in `config.yaml`) — the only way to reach it is through Home
Assistant's own Ingress proxy, which requires a logged-in, admin-level
Home Assistant user (`panel_admin: true`). The add-on's HTTP server never
authenticates requests itself; it relies entirely on that Ingress layer,
the same model every other Home Assistant add-on with a web UI uses.

The write endpoints (`/api/mappings`, `/api/deploy`, `/api/refresh`) carry
no separate CSRF token, for the same reason: Ingress URLs are per-install
and session-scoped, and nothing outside an authenticated admin session can
reach them. Every payload they do accept is still independently bounded in
size and re-validated against the engine's own rules before anything is
written or deployed.

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
them with `direction: git_to_ha`. They can still be mapped for comparison,
and for `direction: ha_to_git` — that direction only ever reads them, it
never writes to Home Assistant, so the protection (which is specifically
about never overwriting these files) does not apply to it.

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

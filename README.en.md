# Homelab Git Management

🇫🇷 [Version française](README.md)

Controlled, one-way or two-way sync between a GitHub repository and your
Home Assistant configuration: clone → compare → review → confirm →
deploy/push → verify → automatic rollback on failure.

[![Buy me a beer](https://img.shields.io/badge/Buy%20me%20a%20beer-%F0%9F%8D%BA-orange?style=for-the-badge)](https://www.buymeacoffee.com/Monsieurgg)

## Why this add-on?

Generative-AI coding tools (assistants able to write YAML, Lovelace
dashboards, automations, and more) can now generate or edit your Home
Assistant configuration directly inside a connected GitHub repository.
That's powerful, but it raises a real question: how do you get that
generated content into a *live* Home Assistant instance, without ever
risking your running configuration, and without the round trip being
painful to test?

That's exactly what this add-on is for: a safe, explicit bridge between
Git and Home Assistant. You (or your AI) write and commit to the
repository; this add-on compares every mapped file against your real
configuration, shows you precisely what differs, and only ever deploys
what you explicitly confirm — with an automatic backup and rollback if
anything goes wrong. The result is a fast, low-risk loop between
generating content and testing it for real, as often as you need.

## This is a Home Assistant Application (Add-on) — not a HACS integration

This distinction matters, because it changes how you install this project:

- **HACS** distributes `custom_components` — Python integrations that run
  *inside* the Home Assistant Core process and add entities/devices.
- **This project is an Application/Add-on**: a separate Docker container,
  managed by the Supervisor, with its own web UI reachable through
  Home Assistant's Ingress. It never runs inside Home Assistant Core and
  it has no `custom_components` folder, no `manifest.json`, no config
  flow — none of that applies here.

That also means you will **not** find this add-on by searching inside HACS,
and it is not installed through it. See [Installation](#installation) below
for the real procedure.

## What it does

1. Generates its own SSH keypair on first start and shows you the public
   half in its web UI, ready to paste into GitHub as a **read-only**
   Deploy Key.
2. Clones your repository (read-only) into a persistent volume.
3. Compares each file/directory you map between the Git clone and your
   live Home Assistant configuration (byte-for-byte, with a lenient
   "equivalent" classification for line-ending/BOM-only differences).
4. Shows you the result and, only for mappings you configured with
   `direction: git_to_ha` and where a real difference exists, offers a
   **Deploy** button.
5. On click, after a browser confirmation, it backs up the current file,
   writes the new one atomically, verifies the result byte-for-byte, and
   automatically restores the backup if anything about that verification
   doesn't check out.

Nothing is compared or deployable until you explicitly configure it — the
default is to manage nothing at all.

## Installation

One-click add (requires [My Home Assistant](https://my.home-assistant.io/)
to be linked to your instance):

[![Add this repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMonsieurgg%2Fha-git-management)

Or manually:

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top right) → **Repositories**.
3. Add this URL: `https://github.com/Monsieurgg/ha-git-management`
4. Find **Homelab Git Management** in the store and install it.
5. Start it once. Open its **Web UI** (Ingress panel) — it will show a
   setup screen with a public SSH key.
6. On GitHub, go to your repository → **Settings → Deploy keys → Add deploy
   key**, paste the key, and leave "Allow write access" **unchecked**
   (this add-on only ever needs read access).
7. Back in Home Assistant, open the add-on's **Configuration** tab and set:
   - `github_repository`: `owner/repository`
   - `github_branch`: e.g. `main`
   - `mappings`: the files/directories you want it to manage (see below) —
     or use the **+ Add a mapping** button directly in the add-on's own
     dashboard, with a built-in file browser.
8. Restart the add-on.

## Configuring mappings

Each mapping is:

```yaml
mappings:
  - id: dashboard_main
    kind: file            # "file" (default) or "directory"
    ha_path: /config/dashboard.yaml
    git_path: home-assistant/dashboard.yaml
    direction: git_to_ha   # git_to_ha, ha_to_git, or bidirectional
```

- `id`: a short identifier, letters/digits/`_`/`-` only, used as the
  deploy target.
- `ha_path`: absolute path under `/config` (your live Home Assistant
  configuration directory).
- `git_path`: path relative to the repository root.
- `direction`: `git_to_ha`, `ha_to_git`, or `bidirectional` — all three
  are implemented. `ha_to_git` pushes to GitHub through a second,
  dedicated write-capable SSH key, kept separate from the read-only key.
  `bidirectional` combines both directions on the same file: the add-on
  offers Deploy or Push depending on which side actually changed, and
  detects real conflicts (both sides changed independently) instead of
  silently overwriting an edit made directly on GitHub. See `DOCS.md` for
  the full detail.

Protection against `git_to_ha` deployment is now a per-mapping choice (a
checkbox in the dashboard's mapping form), not limited to a fixed list.
By default, Home Assistant's own `configuration.yaml`, `scripts.yaml`,
`automations.yaml` and `scenes.yaml` stay protected with no action
needed — but that default can be turned off explicitly (with a strong
warning and an extra confirmation), and any other file can just as
easily be protected the same way. See `DOCS.md`.

## Security

- GitHub is only ever reached read-only, through a Deploy Key generated
  and stored entirely inside this add-on's own persistent data volume —
  never baked into the image, never leaves the add-on;
- the Git clone is only ever fast-forwarded; a locally modified or
  diverged clone makes the add-on refuse to continue;
- every deployment requires, at the same time: a mapping with
  `direction: git_to_ha`, a real detected difference, and an explicit
  confirmation click in the browser;
- every write is preceded by a persistent backup, performed atomically
  (`os.replace`), and verified byte-for-byte; a failed verification
  triggers an automatic rollback;
- directory deployment is intentionally not supported (comparison only) —
  a deliberate reduction of risk;
- the web UI never displays file contents or secrets, only comparison
  state — with one deliberate, narrow exception: the conflict resolution
  screen, and its "Preview the difference" counterpart for a normal
  Deploy/Push, show a line-by-line difference to help you decide (never
  transmitted anywhere beyond your own authenticated Ingress session);
- the add-on has no network port of its own: it is only reachable through
  Home Assistant's Ingress, which requires an authenticated admin user
  session. See
  [`homelab_git_management/DOCS.md`](homelab_git_management/DOCS.md) for
  the full exposure model.

## Support this project

If this saves you from copy-pasting YAML by hand, you can buy me a beer:
[buymeacoffee.com/Monsieurgg](https://www.buymeacoffee.com/Monsieurgg).

## Finding this add-on

There is no HACS-style automatic discovery for third-party Home Assistant
Add-on repositories — you (or anyone using this) always add the repository
URL manually once, as described above. See `DOCS.md` for more on
visibility and where this kind of project can realistically be listed.

## Documentation

See [`homelab_git_management/DOCS.md`](homelab_git_management/DOCS.md) for
the full add-on documentation and
[`CHANGELOG.md`](homelab_git_management/CHANGELOG.md) for the version
history.

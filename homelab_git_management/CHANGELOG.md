# Changelog

All notable changes to this add-on are documented here.

## 0.2.0

- New: mappings can now be created, edited and deleted directly from the
  Ingress dashboard, with a built-in directory/file browser for both
  the Home Assistant side and the Git side — no more typing raw paths
  by hand in the native Configuration tab (which remains fully usable
  as before, nothing is removed there).
- The browser lists real directories, read-only, confined to the
  Home Assistant config mount and the local Git clone respectively;
  dotfiles/dotdirs (e.g. `.storage`, `.git`) are hidden by default.
- Every mapping submitted through the new dashboard form is re-validated
  with the exact same rules the engine itself enforces (identifier
  format, allowed kind, implemented direction, path containment, the
  four protected core Home Assistant files) before it is ever saved —
  nothing is accepted here that the engine would refuse to load.
- This is the first feature where the add-on writes its own
  configuration (via the Supervisor API, scoped to itself only) instead
  of only reading it; see DOCS.md for the security notes.

## 0.1.4

- Fixed a real gap found during end-to-end testing: the engine's
  `[BACKUP]` / `[DEPLOY]` / `[ROLLBACK]` log lines were silently
  discarded when triggered from the Ingress UI (Refresh / Deploy
  buttons), because their output was captured only to build an error
  message and never forwarded. They now always reach the add-on's real
  logs (success or failure), while the routine background reload every
  5 seconds (status polling) stays silent as intended.

## 0.1.3

- The "Open the Configuration tab" link now sits inline, right next to
  "Configured repository", instead of as a separate block below the
  setup steps — easier to notice when that field reads "not set".

## 0.1.2

- The first-time setup screen now shows an "Open the Configuration tab"
  link (only while no repository is configured), pointing to this
  add-on's own native Configuration page. The link is built from a
  read-only Supervisor API lookup of the add-on's own slug — no new
  write capability, nothing else changes.

## 0.1.1

- First-time setup screen rewritten as four numbered steps instead of a
  single paragraph, with a "Copy" button next to the public key.

## 0.1.0 — Initial public release

- First public release, generalized from a private single-user build:
  the managed-files list is now a user-configurable option
  (`mappings`) instead of a file baked into the image.
- First-run setup screen: the add-on generates its own Deploy Key and
  displays the public half in the Ingress UI until a repository is
  configured.
- `github_repository` / `github_branch` options replace a hardcoded
  repository URL and branch.
- Comparison, atomic deployment, backup and automatic rollback engine
  carried over unchanged in behavior from earlier private development.
- `direction: git_to_ha` is the only implemented sync direction;
  `ha_to_git` and `bidirectional` are accepted by the options schema for
  forward compatibility but rejected at startup with an explicit error.
- Home Assistant's `configuration.yaml`, `scripts.yaml`,
  `automations.yaml` and `scenes.yaml` are always protected against
  `git_to_ha` deployment.
- Directory deployment remains unsupported (comparison only).
- Ingress UI available in English and French.

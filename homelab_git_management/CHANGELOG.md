# Changelog

All notable changes to this add-on are documented here.

## 0.2.4

- Real-world testing showed the local options.json this add-on reads
  can lag well past a few seconds after Supervisor accepts a mapping
  save — a manual restart from the Info tab reliably fixed it. This is
  now automatic: if a save isn't visible within ~1.5 seconds, the
  add-on restarts itself (the same "self"-scoped Supervisor API used
  for the options write, so it can only ever restart this add-on) and
  the dashboard shows a brief spinner overlay while it comes back, then
  refreshes on its own — no manual restart needed anymore.

## 0.2.3

- Fixed a UX regression introduced in 0.2.1/0.2.2: a mapping save that
  Supervisor had genuinely accepted could still show a red "not visible
  in time" error a few seconds later, with no clear next step (Cancel?
  close?). Supervisor's own success response is now treated as the
  real confirmation — the dialog always closes on a successful save,
  and if the local view hasn't caught up yet, the regular 5-second
  refresh picks it up on its own, no action needed. The brief local
  wait (now 3s) still makes the common, fast case feel instant; it is
  no longer able to turn a real success into an error.

## 0.2.2

- Raised the wait for a mapping save to become visible from 4 to 12
  seconds: real-world testing showed Supervisor's options write can
  take longer than 4 seconds to land, which was surfacing the
  "not visible in time" message for saves that actually succeeded a
  moment later anyway.

## 0.2.1

- Fixed a real bug found during testing: saving a mapping from the
  dashboard could report success while the new configuration silently
  failed to appear — Home Assistant Supervisor's options-save API can
  return success slightly before the configuration this add-on reads
  is actually updated. Saving now waits for the change to become
  visible (a few hundred milliseconds, typically) before confirming;
  if it still isn't visible after a few seconds, the add-on now says so
  explicitly instead of showing a stale, unexplained state.
- New validation when saving a mapping: the HA path and Git path must
  now agree with the chosen `kind` (a "file" mapping can't point at an
  existing directory, and vice versa), and for `kind: file`, both paths
  must share the same file extension. Both catch a mismatched pick
  immediately, with a clear message, instead of failing later as an
  opaque engine error.
- The directory browser now recovers automatically if reopened on a
  field that already holds a file path (not a directory): it backs up
  to the containing folder instead of showing an error.

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

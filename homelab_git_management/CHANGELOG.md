# Changelog

All notable changes to this add-on are documented here.

## 1.4.0 — normalize equivalent to identical

- New kebab-menu action, "Make identical", shown for any `ha_to_git` or
  `bidirectional` mapping currently classified **equivalent** (same
  content once a UTF-8 byte-order mark, line endings and a trailing
  newline are normalized away, but not byte-for-byte the same). It takes
  Home Assistant's exact bytes and writes them into Git as a real commit,
  after explicit confirmation — turning "equivalent" into "identical".
- Reuses the exact same write/commit/push/verify/automatic-rollback
  machinery as a normal `ha_to_git` push (including the second,
  write-capable Deploy Key), with two differences: it only accepts a
  mapping in the "equivalent" state (a real content difference still
  goes through the normal Push button), and it deliberately skips the
  line-ending-mismatch guard — normalizing the line ending is the whole
  point of this action, not an accident it should catch. The resulting
  commit message says explicitly that this is a formatting-only change,
  not a content update.
- Not offered for `git_to_ha` mappings: that direction never writes to
  Git, so there is nothing for it to normalize.

## 1.3.0 — configurable per-mapping protection

- Protection against Git → Home Assistant overwrites is now a per-mapping
  choice (`protect_from_git: true/false`), set from a checkbox in the
  dashboard's mapping form, instead of being hardcoded for exactly four
  files. Any mapping can now be protected, not just Home Assistant's own
  `configuration.yaml` / `scripts.yaml` / `automations.yaml` /
  `scenes.yaml`.
- Those four files remain protected **by default** — nothing changes for
  existing mappings, or new ones, unless someone explicitly touches the
  checkbox — but this default can now be turned off. The dashboard shows
  a strong warning banner the moment protection is off on one of these
  four files, and requires an extra explicit confirmation before saving
  that state, on top of the confirmation the Deploy button itself already
  asks for. This is an intentional, informed choice to give up a real
  safety net; there is no hidden default that silently restores it later.
- When adding a mapping through the dashboard, the checkbox defaults to
  checked automatically as soon as the Home Assistant path matches one of
  those four files, and the checkbox only appears at all for a mapping
  whose direction can actually write to Home Assistant (`git_to_ha` or
  `bidirectional` — it has no effect on a pure `ha_to_git` mapping).
- A mapping added directly through the native Configuration tab (raw
  YAML, no `protect_from_git` field at all) keeps exactly the previous
  behavior: protected by default for those four files, unprotected for
  everything else.

## 1.2.0 — bidirectional

- New: `direction: bidirectional` is now implemented. A mapping
  configured this way can go either way — Deploy (Git → Home Assistant)
  or Push (Home Assistant → Git) — with the add-on deciding which button
  to offer based on which side actually changed since the last sync, not
  just on raw content difference. This reuses the same reference-based
  conflict detection built for `ha_to_git`, applied to both directions
  instead of one.
- Only Home Assistant changed → Push is offered. Only Git changed →
  Deploy is offered. Both changed independently → neither is offered
  automatically; the mapping shows a **CONFLICT** badge and a **Resolve**
  button, with the same dates + line-by-line diff screen as `ha_to_git`,
  now with two real resolutions: force-deploy Git → Home Assistant, or
  force-push Home Assistant → Git. Both actually write — there is no
  passive "acknowledge without syncing" option for `bidirectional`,
  since leaving the two sides mismatched would just recreate the same
  difference on the next comparison.
- A brand-new `bidirectional` mapping with no sync history yet and
  already-divergent content starts as a conflict too, requiring one
  explicit, forced resolution to establish which side is authoritative —
  this add-on never guesses which of two unrelated histories to trust.
- `configuration.yaml` / `scripts.yaml` / `automations.yaml` /
  `scenes.yaml` can be mapped with `direction: bidirectional`, but the
  Deploy side (Git → Home Assistant) remains permanently blocked for
  these four files, exactly as for `git_to_ha` — only the Push side
  (Home Assistant → Git) is available for them.
- The line-ending safety check and the write-capable Deploy Key
  requirement introduced for `ha_to_git` apply identically to
  `bidirectional`'s Push side — nothing new to configure if you already
  set up `ha_to_git`.
- No change to `git_to_ha` or `ha_to_git` behavior when used on their
  own.

## 1.1.0 — ha_to_git

- New: `direction: ha_to_git` is now implemented. A mapping configured
  this way can push the current Home Assistant file to GitHub, as a real
  commit, after explicit confirmation — the reverse of the existing
  `git_to_ha` deployment.
- A second, entirely separate SSH Deploy Key is generated for this
  (`/data/ssh/github_deploy_key_write`), shown in its own section of the
  Ingress UI. The original read-only key is completely unchanged and is
  never used by any write path — ha_to_git stays inactive until this new
  key is explicitly added on GitHub with write access enabled.
- Safety against silently discarding an external edit: the add-on now
  remembers, per mapping, what each side looked like the last time they
  were confirmed in agreement (`/data/sync-state.json`). If Git changed
  outside this add-on since then (most likely edited directly on GitHub)
  while Home Assistant also changed, that is a real conflict — the Push
  button is replaced with a "Resolve" button showing both sides' dates
  and a line-by-line difference, and the push is blocked until a human
  either force-pushes the Home Assistant version or accepts the current
  Git content as the new reference point. A plain "most recent wins"
  comparison cannot make this distinction and can silently overwrite a
  real edit — this add-on never does that.
- The diff view (in that Resolve screen only) is the one place this
  add-on now shows real file content, instead of only comparison
  metadata — scoped strictly to ha_to_git conflicts, capped in size, and
  skipped entirely for binary content. DOCS.md documents this exception
  explicitly.
- Found during real-world testing: if the Home Assistant file already has
  different line endings than what is tracked in Git (typically because
  something upstream — an editor, a network share, ...— touched the live
  file), pushing it as-is silently converts every line and buries the
  real change in a diff touching the whole file. A push is now blocked
  with a clear error whenever the two sides' line-ending conventions
  disagree, instead of committing that noise.
- `configuration.yaml` / `scripts.yaml` / `automations.yaml` /
  `scenes.yaml` can now be mapped with `direction: ha_to_git` (read-only
  from Home Assistant's side, so the existing protection — which is only
  about never writing back to these files — does not apply here); they
  remain permanently blocked from `git_to_ha` deployment as before.
- No change to git_to_ha, comparison, backup or rollback behavior.

## 1.0.0 — First stable public release

- README rewritten in French (primary, shown by default on GitHub) with an
  English version (`README.en.md`) kept in sync, both explaining the main
  use case this add-on is built around: generative-AI coding tools can
  write or edit Home Assistant dashboards/configuration in a connected
  GitHub repository, and this add-on is the safe, explicit-confirmation
  bridge to bring that generated content into a live Home Assistant
  instance and iterate quickly between the two.
- Security hardening pass ahead of the stable release: `/api/deploy` now
  enforces the same 256 KiB request-size cap `/api/mappings` already had
  (it previously read the request body with no upper bound), and a
  malformed `Content-Length` header on either endpoint is now rejected
  with a clean error instead of crashing the request. No externally
  reachable vulnerability was found in this pass — this closes a
  robustness gap, not an exploit.
- DOCS.md now documents this add-on's exposure model explicitly (Ingress-
  only, no LAN port, admin-only panel, no separate CSRF token because
  Ingress sessions already are per-install and authenticated) instead of
  leaving it implicit.
- No functional or behavioral change to the comparison, deployment,
  backup or rollback engine — this release is documentation and
  hardening only, on top of the 0.2.x feature set below.

## 0.2.6

- The "Manage" column now shows the Deploy button directly when it
  applies, and everything else (Edit, Delete, and future actions) is
  tucked behind a small "⋮" menu instead of a row of separate buttons —
  keeps the table from getting more crowded as more actions get added
  over time.

## 0.2.5

- Merged the two dashboard tables ("Managed elements status" and
  "Configured mappings") into one: each row now shows the HA path, the
  Git path, the comparison state and the Edit/Delete/Deploy actions
  together, instead of the same mapping appearing twice across two
  separate tables. Dropped the now-redundant Type and Direction
  columns (shown instead as a small icon next to the element name, and
  folded into the comparison cell only when relevant) to keep the
  table from feeling as crowded.

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

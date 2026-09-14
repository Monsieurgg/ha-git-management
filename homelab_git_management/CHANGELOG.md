# Changelog

All notable changes to this add-on are documented here.

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

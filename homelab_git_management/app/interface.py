#!/usr/bin/env python3

###############################################################################
# HOMELAB GIT MANAGEMENT — INTERFACE
###############################################################################
#
# Role:
#   Serve the Ingress web UI for Homelab Git Management.
#
# Two modes:
#   - setup mode: no github_repository option is configured yet. The page
#     shows the generated Deploy Key's public half so the user can paste
#     it into GitHub -> Settings -> Deploy Keys, plus the exact option
#     fields to fill in afterwards.
#   - dashboard mode: a repository is configured. The page shows the
#     Git <-> Home Assistant comparison state for every mapping and lets
#     the user trigger a Git refresh (read-only) or a deployment
#     (write, gated on browser confirm() first).
#
# Security:
#   - no file content is ever displayed, no secret, only metadata;
#   - only the *public* half of the Deploy Key is ever shown;
#   - deployment is only proposed for elements the engine itself marks
#     deployable_now (not protected, direction: git_to_ha, state
#     actually different); /api/deploy reuses deployer_element(), so no
#     rule is duplicated here;
#   - Home Assistant's own Ingress layer handles who can reach this page.
#
###############################################################################

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
import contextlib
import difflib
import importlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Used only to generate/read the bulk mappings Excel workbook (export and
# import). No other part of this add-on depends on it: /data/options.json
# itself is still plain JSON via the standard library, and the engine
# (gestionnaire.py) has no notion of Excel at all.
import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


###############################################################################
# CONSTANTS
###############################################################################

HOST = "0.0.0.0"
PORT = 8099

APP_VERSION = os.environ.get("APP_VERSION", "unknown")

HA_ROOT = Path("/homeassistant")
GIT_ROOT = Path("/data/repository")
OPTIONS_PATH = Path("/data/options.json")
SSH_PUBLIC_KEY = Path("/data/ssh/github_deploy_key.pub")
SSH_PUBLIC_KEY_WRITE = Path("/data/ssh/github_deploy_key_write.pub")

# The one place this add-on ever sends real file content to the browser,
# instead of just comparison metadata — and only for this one narrow
# purpose: helping a user resolve a ha_to_git conflict/external-change
# state by seeing what actually differs. Never used for any other
# mapping or state. See gerer_diff() and DOCS.md.
DIFF_MAX_BYTES = 262144  # 256 KiB per side

# Bulk mappings export/import (Excel). A workbook this size is already far
# beyond anything a real mappings list would ever need — this only guards
# against an unrelated, mistakenly-uploaded file.
EXCEL_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

# How many distinct file/directory names are offered in the export's
# dropdown lists, per side (HA, Git). A cap on the *dropdown convenience*
# only — resolving a name typed or picked from an import always searches
# the full tree, uncapped, regardless of this limit.
EXCEL_DROPDOWN_MAX_ENTRIES = 2000

# Extra blank, dropdown-ready rows appended after the current mappings in
# an export, ready to fill in for a bulk add.
EXCEL_TEMPLATE_BLANK_ROWS = 30

SUPERVISOR_API = "http://supervisor"

# Never shown in the directory browser: dotfiles/dotdirs (.git, .storage,
# .cloud, .ssh, ...) often hold internal state or secrets, never a
# plausible mapping target — hiding them is a safety default, not a
# missing feature.
NOMS_IGNORES_NAVIGATION = {"__pycache__"}


###############################################################################
# HTML PAGE
###############################################################################

HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Homelab Git Management</title>
<style>
:root {
  --bg: #f4f5f7; --surface: #ffffff; --surface-soft: #f8fafc;
  --text: #1f2937; --muted: #6b7280; --border: #e5e7eb; --accent: #2563eb;
  --ok-bg: #dcfce7; --ok: #166534; --info-bg: #e0f2fe; --info: #075985;
  --warn-bg: #ffedd5; --warn: #9a3412; --danger-bg: #fee2e2; --danger: #991b1b;
  --neutral-bg: #f3f4f6; --neutral: #4b5563;
  --candidate-bg: #fef3c7; --candidate: #92400e;
  --shadow: 0 8px 24px rgba(0,0,0,0.07);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrapper { max-width: 1400px; margin: 0 auto; padding: 22px; }
.header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 18px; flex-wrap: wrap; }
.header-title h1 { margin: 0; font-size: 25px; line-height: 1.2; }
.header-title p { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.header-actions { display: flex; gap: 8px; align-items: center; }
.header-link { border: 1px solid var(--border); background: var(--surface); color: var(--text);
  border-radius: 10px; padding: 10px 15px; font: inherit; text-decoration: none;
  display: inline-flex; align-items: center; line-height: 1; }
.header-link:hover { border-color: var(--accent); }
button { border: 1px solid var(--border); background: var(--surface); color: var(--text);
  border-radius: 10px; padding: 10px 15px; cursor: pointer; font: inherit; }
button:hover { border-color: var(--accent); }
button:disabled { opacity: 0.5; cursor: default; }
.action-deploy { margin-right: 6px; padding: 4px 10px; font-size: 12px; border-color: var(--candidate); color: var(--candidate); }
.action-push { margin-right: 6px; padding: 4px 10px; font-size: 12px; border-color: var(--accent); color: var(--accent); }
.action-resolve { margin-right: 6px; padding: 4px 10px; font-size: 12px; border-color: var(--danger); color: var(--danger); }
.direction-select { padding: 4px 8px; font-size: 12px; border-radius: 8px; }
.summary { display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 15px; box-shadow: var(--shadow); }
.card-label { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.card-value { margin-top: 6px; font-size: 26px; font-weight: 700; }
.card-meta { margin-top: 5px; color: var(--muted); font-size: 12px; }
.panel { overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); margin-bottom: 18px; }
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 15px 17px; border-bottom: 1px solid var(--border); }
.panel-header h2 { margin: 0; font-size: 17px; }
.panel-header-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
#keys-panel > summary { padding: 15px 17px; font-weight: 600; font-size: 15px; cursor: pointer;
  list-style: none; }
#keys-panel > summary::-webkit-details-marker { display: none; }
#keys-panel[open] > summary { border-bottom: 1px solid var(--border); }
.panel-body { padding: 17px; }
.status-line { color: var(--muted); font-size: 12px; text-align: right; }
.table-wrapper { overflow-x: auto; }
table { width: 100%; min-width: 900px; border-collapse: collapse; }
th, td { padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; font-size: 13px; vertical-align: middle; }
th { background: var(--surface-soft); color: var(--muted); font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.key-box { display: block; width: 100%; padding: 12px; background: var(--surface-soft); border: 1px solid var(--border);
  border-radius: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
  word-break: break-all; white-space: pre-wrap; user-select: all; }
.key-row { display: flex; gap: 8px; align-items: flex-start; }
.key-row .key-box { flex: 1; }
.key-row button { flex-shrink: 0; white-space: nowrap; }
.setup-steps { margin: 0 0 18px; padding-left: 22px; }
.setup-steps li { margin-bottom: 10px; line-height: 1.5; }
.config-link-inline { margin-left: 10px; color: var(--accent); text-decoration: underline; font-weight: 600; font-size: 13px; }
.config-link-inline:hover { text-decoration: none; }
.field-row { margin: 10px 0; font-size: 13px; }
.field-row .label { color: var(--muted); margin-right: 6px; }
.badge { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 8px; white-space: nowrap; font-size: 11px; font-weight: 700; }
.identical { background: var(--ok-bg); color: var(--ok); }
.equivalent { background: var(--info-bg); color: var(--info); }
.different { background: var(--warn-bg); color: var(--warn); }
.blocked { background: var(--danger-bg); color: var(--danger); }
.neutral { background: var(--neutral-bg); color: var(--neutral); }
.candidate { background: var(--candidate-bg); color: var(--candidate); }
.error { display: none; margin-bottom: 16px; padding: 13px 15px; background: var(--danger-bg); color: var(--danger);
  border: 1px solid #fecaca; border-radius: 12px; white-space: pre-wrap; }
.readonly-banner { margin-bottom: 16px; padding: 10px 14px; color: var(--info); background: var(--info-bg); border-radius: 10px; font-size: 13px; }
[hidden] { display: none !important; }
@media (max-width: 1100px) { .summary { grid-template-columns: repeat(3, minmax(0,1fr)); } }
@media (max-width: 650px) {
  .wrapper { padding: 12px; }
  .header { align-items: flex-start; flex-direction: column; }
  .summary { grid-template-columns: repeat(2, minmax(0,1fr)); }
}
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.modal-overlay { display: flex; align-items: center; justify-content: center; position: fixed; inset: 0;
  background: rgba(15,23,42,0.45); z-index: 50; padding: 16px; }
.modal { background: var(--surface); border-radius: 14px; box-shadow: var(--shadow); width: 100%;
  max-width: 480px; max-height: 88vh; display: flex; flex-direction: column; overflow: hidden; }
.modal-wide { max-width: 720px; }
.conflict-dates { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; font-size: 13px; }
.conflict-dates div { background: var(--surface-soft); border: 1px solid var(--border); border-radius: 10px;
  padding: 8px 12px; }
.conflict-dates .label { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 3px; }
.diff-view { margin: 0; padding: 12px; background: var(--surface-soft); border: 1px solid var(--border);
  border-radius: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
  line-height: 1.5; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto; }
.diff-add { background: var(--ok-bg); color: var(--ok); display: block; }
.diff-remove { background: var(--danger-bg); color: var(--danger); display: block; }
.diff-hunk { color: var(--info); display: block; }
.plan-line { padding: 2px 6px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; margin-bottom: 2px; }
.plan-add { background: var(--ok-bg); color: var(--ok); }
.plan-update { background: var(--warn-bg); color: var(--warn); }
.plan-delete { background: var(--danger-bg); color: var(--danger); }
.plan-section-title { font-weight: 600; margin: 10px 0 6px; font-size: 12px; text-transform: uppercase;
  color: var(--muted); }
.plan-section-title:first-child { margin-top: 0; }
.conflict-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
.backup-row { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; margin-bottom: 8px; }
.backup-row .backup-meta { font-size: 13px; }
.backup-row .backup-size { color: var(--muted); font-size: 12px; }
#backups-list { max-height: 360px; overflow-y: auto; }
.modal-header { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 15px 17px; border-bottom: 1px solid var(--border); }
.modal-header h3 { margin: 0; font-size: 16px; }
.modal-close { border: none; background: none; font-size: 20px; line-height: 1; padding: 4px 8px; }
.modal-body { padding: 17px; overflow-y: auto; }
.modal-footer { padding: 13px 17px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
.form-label { display: block; margin: 14px 0 6px; font-size: 12px; font-weight: 600; color: var(--muted); }
.form-label:first-child { margin-top: 0; }
.form-input { width: 100%; padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px;
  font: inherit; background: var(--surface-soft); color: var(--text); }
.form-input:disabled { opacity: 0.6; }
.path-row { display: flex; gap: 8px; }
.path-row .form-input { flex: 1; }
.breadcrumb { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
  color: var(--muted); margin-bottom: 10px; word-break: break-all; }
.browse-list-wrapper { max-height: 320px; overflow-y: auto; border: 1px solid var(--border); border-radius: 10px; }
.browse-list { list-style: none; margin: 0; padding: 0; }
.browse-entry { padding: 9px 12px; cursor: pointer; font-size: 13px; border-bottom: 1px solid var(--border); }
.browse-entry:last-child { border-bottom: 0; }
.browse-entry:hover { background: var(--surface-soft); }
.browse-up { color: var(--muted); font-weight: 600; }
.kebab-wrapper { position: relative; display: inline-block; }
.kebab-btn { padding: 4px 10px; font-size: 16px; line-height: 1; }
.kebab-menu { position: absolute; right: 0; top: 100%; margin-top: 4px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow); min-width: 140px;
  z-index: 20; overflow: hidden; }
.kebab-menu button { display: block; width: 100%; text-align: left; border: none; border-radius: 0;
  padding: 9px 12px; background: var(--surface); font-size: 13px; }
.kebab-menu button:hover { background: var(--surface-soft); border-color: transparent; }
.kebab-menu button.danger { color: var(--danger); }
.restart-overlay { position: fixed; inset: 0; background: rgba(255,255,255,0.94); z-index: 100;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px;
  text-align: center; padding: 24px; }
.restart-overlay p { margin: 0; color: var(--text); font-size: 14px; max-width: 320px; }
.spinner { width: 38px; height: 38px; border: 4px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="wrapper">

  <div class="header">
    <div class="header-title">
      <h1 data-i18n="title">Homelab Git Management</h1>
      <p data-i18n="subtitle">Git &lt;-&gt; Home Assistant control</p>
    </div>
    <div class="header-actions">
      <a class="header-link" href="https://www.buymeacoffee.com/Monsieurgg" target="_blank" rel="noopener" data-i18n="buy_me_a_beer">☕ Buy me a beer</a>
      <button id="lang-toggle" type="button"></button>
      <button id="refresh" data-i18n="refresh" hidden>Refresh</button>
    </div>
  </div>

  <div id="error" class="error"></div>

  <div id="setup-panel" class="panel" hidden>
    <div class="panel-header"><h2 data-i18n="setup_title">First-time setup</h2></div>
    <div class="panel-body">
      <ol class="setup-steps">
        <li data-i18n="setup_step1">Copy the public key below.</li>
        <li data-i18n="setup_step2">On GitHub, open this repository's own Settings -> Deploy keys -> Add deploy key (not your personal account settings).</li>
        <li data-i18n="setup_step3">Paste the key, leave "Allow write access" unchecked, and save.</li>
        <li data-i18n="setup_step4">Come back here, open this add-on's Configuration tab, set github_repository (owner/repository) and github_branch, then restart it.</li>
      </ol>
      <div class="field-row"><span class="label" data-i18n="setup_key_label">Public key</span></div>
      <div class="key-row">
        <code id="setup-key" class="key-box">—</code>
        <button id="copy-key" type="button" data-i18n="copy_key">Copy</button>
      </div>
      <div class="field-row">
        <span class="label" data-i18n="setup_repo_label">Configured repository</span><span id="setup-repo">—</span>
        <a id="setup-config-link" class="config-link-inline" target="_top" hidden data-i18n="setup_open_config">Open the Configuration tab →</a>
      </div>
      <div class="field-row"><span class="label" data-i18n="setup_branch_label">Configured branch</span><span id="setup-branch">—</span></div>
    </div>
  </div>

  <details id="keys-panel" class="panel" hidden>
    <summary data-i18n="keys_panel_title">🔑 Deploy keys (read + write)</summary>
    <div class="panel-body">
      <div class="field-row"><span class="label" data-i18n="setup_key_label">Public key</span></div>
      <div class="key-row">
        <code id="setup-key-collapsed" class="key-box">—</code>
        <button id="copy-key-collapsed" type="button" data-i18n="copy_key">Copy</button>
      </div>
      <p class="field-row" data-i18n="write_key_intro"></p>
      <div class="field-row"><span class="label" data-i18n="setup_write_key_label">Write-capable public key</span></div>
      <div class="key-row">
        <code id="setup-key-write" class="key-box">—</code>
        <button id="copy-key-write" type="button" data-i18n="copy_key">Copy</button>
      </div>
    </div>
  </details>

  <div id="dashboard" hidden>

    <div class="readonly-banner" data-i18n="readonly_banner"></div>

    <div class="summary">
      <div class="card">
        <div class="card-label" data-i18n="card_app">Application</div>
        <div id="version" class="card-value">—</div>
        <div class="card-meta" data-i18n="card_app_meta">runtime version</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n="card_managed">Managed elements</div>
        <div id="managed" class="card-value">—</div>
        <div class="card-meta" data-i18n="card_managed_meta">mappings</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n="card_identical">Identical</div>
        <div id="identical" class="card-value">—</div>
        <div class="card-meta" data-i18n="card_identical_meta">strict equality</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n="card_different">Different</div>
        <div id="different" class="card-value">—</div>
        <div class="card-meta" data-i18n="card_different_meta">actual content</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n="card_candidates">To deploy</div>
        <div id="candidates" class="card-value">—</div>
        <div class="card-meta" data-i18n="card_candidates_meta">allowed candidates</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2 data-i18n="table_title">Managed elements</h2>
        <div class="panel-header-actions">
          <div id="status-line" class="status-line" data-i18n="loading">Loading…</div>
          <button id="add-mapping" type="button" data-i18n="mappings_add">+ Add a mapping</button>
          <button id="export-mappings" type="button" data-i18n="mappings_export">Export mappings (Excel)</button>
          <button id="import-mappings" type="button" data-i18n="mappings_import">Import mappings (Excel)</button>
          <input id="import-mappings-input" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden>
        </div>
      </div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th data-i18n="th_element">Element</th>
              <th data-i18n="th_ha_path">HA path</th>
              <th data-i18n="th_git_path">Git path</th>
              <th data-i18n="th_direction">Direction</th>
              <th data-i18n="th_comparison">Comparison</th>
              <th data-i18n="th_manage">Manage</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </div>

  </div>

</div>

<div id="mapping-modal" class="modal-overlay" hidden>
  <div class="modal">
    <div class="modal-header">
      <h3 id="mapping-modal-title" data-i18n="mapping_modal_add">Add a mapping</h3>
      <button id="mapping-modal-close" type="button" class="modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <div id="mapping-error" class="error"></div>

      <label class="form-label" data-i18n="field_id">Identifier</label>
      <input id="mapping-id" type="text" class="form-input" autocomplete="off">

      <label class="form-label" data-i18n="field_kind">Type</label>
      <select id="mapping-kind" class="form-input">
        <option value="file" data-i18n="kind_file">File</option>
        <option value="directory" data-i18n="kind_directory">Directory (comparison only)</option>
      </select>

      <label class="form-label" data-i18n="field_direction">Direction</label>
      <select id="mapping-direction" class="form-input">
        <option value="git_to_ha" data-i18n="dir_git_to_ha">Git -&gt; Home Assistant</option>
        <option value="ha_to_git" data-i18n="dir_ha_to_git">Home Assistant -&gt; Git</option>
        <option value="bidirectional" data-i18n="dir_bidirectional">Bidirectional</option>
      </select>

      <label class="form-label" data-i18n="field_ha_path">Home Assistant path</label>
      <div class="path-row">
        <input id="mapping-ha-path" type="text" class="form-input" placeholder="/config/...">
        <button type="button" class="browse-btn" data-root="ha" data-i18n="browse">Browse…</button>
      </div>

      <label class="form-label" data-i18n="field_git_path">Git path</label>
      <div class="path-row">
        <input id="mapping-git-path" type="text" class="form-input" placeholder="themes/...">
        <button type="button" class="browse-btn" data-root="git" data-i18n="browse">Browse…</button>
      </div>

      <div id="mapping-protect-row" class="field-row" hidden>
        <label>
          <input id="mapping-protect" type="checkbox">
          <span id="mapping-protect-label" data-i18n="field_protect">Protect this file from being overwritten by Git</span>
        </label>
      </div>
      <div id="mapping-protect-warning" class="error"></div>

      <div id="mapping-normalize-row" class="field-row" hidden>
        <label>
          <input id="mapping-normalize-line-endings" type="checkbox">
          <span data-i18n="field_normalize_line_endings">Auto-fix line endings on Push instead of blocking</span>
        </label>
      </div>

      <div class="field-row">
        <label>
          <input id="mapping-create-if-missing" type="checkbox">
          <span data-i18n="field_create_if_missing">Create the file/directory now on any side where it doesn't exist yet</span>
        </label>
      </div>
    </div>
    <div class="modal-footer">
      <button id="mapping-cancel" type="button" data-i18n="cancel">Cancel</button>
      <button id="mapping-save" type="button" class="primary" data-i18n="save">Save</button>
    </div>
  </div>
</div>

<div id="browse-modal" class="modal-overlay" hidden>
  <div class="modal">
    <div class="modal-header">
      <h3 data-i18n="browse_title">Browse</h3>
      <button id="browse-modal-close" type="button" class="modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <div id="browse-error" class="error"></div>
      <div id="browse-breadcrumb" class="breadcrumb"></div>
      <div class="browse-list-wrapper">
        <ul id="browse-list" class="browse-list"></ul>
      </div>
    </div>
    <div class="modal-footer">
      <button id="browse-select-here" type="button" data-i18n="browse_select_here">Select this folder</button>
    </div>
  </div>
</div>

<div id="conflict-modal" class="modal-overlay" hidden>
  <div class="modal modal-wide">
    <div class="modal-header">
      <h3 id="conflict-modal-title" data-i18n="conflict_modal_title">Resolve</h3>
      <button id="conflict-modal-close" type="button" class="modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <div id="conflict-error" class="error"></div>
      <p id="conflict-explain" class="field-row"></p>
      <div id="conflict-dates-row" class="conflict-dates">
        <div><span class="label" data-i18n="conflict_ha_date">Home Assistant, last modified</span><span id="conflict-ha-date">—</span></div>
        <div><span class="label" data-i18n="conflict_git_date">Git, last commit</span><span id="conflict-git-date">—</span></div>
      </div>
      <pre id="conflict-diff" class="diff-view">—</pre>
      <div class="conflict-actions">
        <button id="conflict-keep-git" type="button" data-i18n="conflict_keep_git">Keep the Git version</button>
        <button id="conflict-force-ha" type="button" class="action-resolve" data-i18n="conflict_force_ha">Force-push Home Assistant → Git</button>
      </div>
    </div>
    <div class="modal-footer">
      <button id="conflict-cancel" type="button" data-i18n="cancel">Cancel</button>
    </div>
  </div>
</div>

<div id="backups-modal" class="modal-overlay" hidden>
  <div class="modal modal-wide">
    <div class="modal-header">
      <h3 data-i18n="backups_modal_title">Restore a previous backup</h3>
      <button id="backups-modal-close" type="button" class="modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <div id="backups-error" class="error"></div>
      <p class="field-row" data-i18n="backups_explain">Each entry below is a snapshot of this file taken automatically right before a previous deployment overwrote it. Restoring writes it back to Home Assistant now — the current content is itself backed up first, so this is always undoable too.</p>
      <div id="backups-list"></div>
    </div>
    <div class="modal-footer">
      <button id="backups-cancel" type="button" data-i18n="cancel">Cancel</button>
    </div>
  </div>
</div>

<div id="restart-overlay" class="restart-overlay" hidden>
  <div class="spinner"></div>
  <p data-i18n="restart_overlay_text">Applying your change — the add-on is restarting…</p>
</div>

<script>
const STRINGS = {
  en: {
    title: "Homelab Git Management",
    subtitle: "Git <-> Home Assistant control",
    refresh: "Refresh", refreshing: "Refreshing...",
    buy_me_a_beer: "☕ Buy me a beer",
    readonly_banner: "The Refresh button updates the Git clone (read-only from GitHub). The Deploy button, shown only for allowed elements, writes to Home Assistant after explicit confirmation.",
    card_app: "Application", card_app_meta: "runtime version",
    card_managed: "Managed elements", card_managed_meta: "mappings",
    card_identical: "Identical", card_identical_meta: "strict equality",
    card_different: "Different", card_different_meta: "actual content",
    card_candidates: "To deploy", card_candidates_meta: "allowed candidates",
    table_title: "Managed elements", loading: "Loading…",
    th_element: "Element", th_comparison: "Comparison",
    state_identical: "IDENTICAL", state_equivalent: "EQUIVALENT", state_different: "DIFFERENT",
    state_missing_ha: "MISSING ON HA", state_missing_git: "MISSING IN GIT",
    state_missing_both: "MISSING FROM BOTH", state_error: "ERROR", state_unknown: "UNKNOWN",
    protected: "PROTECTED",
    action_deploy: "Deploy",
    action_push: "Push", action_resolve: "Resolve",
    pushing: "Pushing...",
    state_conflict: "CONFLICT", state_external: "EXTERNAL CHANGE",
    error_load: "Could not load status: ", error_refresh: "Git refresh failed: ",
    error_deploy: "Deployment failed: ",
    error_push: "Push failed: ", error_acknowledge: "Could not acknowledge: ",
    error_normalize: "Could not normalize: ",
    deploying: "Deploying...",
    confirm_deploy: 'Deploy "{target}" from GitHub to Home Assistant?\n\nA backup of the current file will be created before writing. If verification fails, an automatic rollback restores the previous content.',
    confirm_push: 'Push "{target}" from Home Assistant to GitHub?\n\nThis creates a real commit on your repository.',
    confirm_force_push: 'Force-push "{target}"? This overwrites whatever is currently on GitHub with the Home Assistant version — the GitHub-side change shown in the diff will be discarded.',
    confirm_force_deploy: 'Force-deploy "{target}"? This overwrites the current Home Assistant file with the GitHub version — the Home Assistant-side change shown in the diff will be discarded.',
    confirm_keep_git: 'Accept the current GitHub content as the new reference point for "{target}"? Nothing is pushed and Home Assistant is not touched — if it still differs afterward, a normal Push becomes available again.',
    confirm_normalize: 'Make "{target}" byte-for-byte identical? This takes the exact Home Assistant version (including its line endings) and writes it into Git as a formatting-only commit — no content is changing, only the byte-level representation.',
    keys_panel_title: "🔑 Deploy keys (read + write)",
    write_key_intro: "Only needed if you configure a mapping with direction: ha_to_git. This is a separate key from the one above — the read-only key never gains write access, and this key stays inactive until you add it on GitHub yourself, this time allowing write access.",
    setup_write_key_label: "Write-capable public key:",
    conflict_modal_title: "Resolve",
    conflict_ha_date: "Home Assistant, last modified:",
    conflict_git_date: "Git, last commit:",
    conflict_keep_git: "Keep the Git version",
    conflict_force_git: "Force-deploy Git → Home Assistant",
    conflict_force_ha: "Force-push Home Assistant → Git",
    conflict_explain_conflict: "Both Home Assistant and Git changed independently since the last sync. Review the difference below, then choose which version to keep.",
    conflict_explain_external: "Git changed outside this add-on (most likely edited directly on GitHub) since the last sync. Pushing now would silently discard that edit.",
    conflict_explain_manual: "Manual comparison: review the difference below, then choose which version to keep. Useful when a normal Push or Deploy is blocked for another reason (for example a line-ending mismatch) and you want to force one side anyway.",
    conflict_explain_preview: "Preview: review the difference below before deciding. Confirming still asks the normal confirmation and does not skip any safety check.",
    backups_modal_title: "Restore a previous backup",
    backups_explain: "Each entry below is a snapshot of this file taken automatically right before a previous deployment overwrote it. Restoring writes it back to Home Assistant now — the current content is itself backed up first, so this is always undoable too.",
    backups_none: "No backup yet for this mapping.",
    backups_restore: "Restore",
    backups_error_load: "Could not load backups: ",
    backups_error_restore: "Could not restore: ",
    confirm_restore_backup: 'Restore this backup of "{target}"? The current Home Assistant content will first be backed up, then overwritten with this older version.',
    conflict_diff_loading: "Loading the difference…",
    conflict_diff_binary: "This file's content cannot be shown as text.",
    conflict_diff_too_large: "This file is too large to preview here.",
    conflict_diff_error: "Could not load the difference: ",
    conflict_diff_none: "No textual difference to show.",
    plan_add: "+ ADD", plan_update: "~ UPDATE", plan_delete: "− DELETE",
    plan_section_git_to_ha: "Git → Home Assistant (Deploy)",
    plan_section_ha_to_git: "Home Assistant → Git (Push)",
    setup_title: "First-time setup",
    setup_step1: "Copy the public key below.",
    setup_step2: "On GitHub, open this repository's own Settings -> Deploy keys -> Add deploy key (not your personal account settings).",
    setup_step3: 'Paste the key, leave "Allow write access" unchecked, and save.',
    setup_step4: "Come back here, open this add-on's Configuration tab, set github_repository (owner/repository) and github_branch, then restart it.",
    setup_key_label: "Public key:",
    setup_repo_label: "Configured repository:",
    setup_branch_label: "Configured branch:",
    setup_none: "not set",
    copy_key: "Copy", copied_key: "Copied!",
    setup_open_config: "Open the Configuration tab →",
    mappings_add: "+ Add a mapping",
    mappings_export: "Export mappings (Excel)", mappings_import: "Import mappings (Excel)",
    mappings_export_error: "Could not generate the Excel file: ",
    mappings_import_error_parse: "This file is not a valid Excel (.xlsx) workbook.",
    mappings_import_error_resolve: "Cannot import this file:\n",
    mappings_import_error_empty: "This file has no mapping rows to import.",
    mappings_import_confirm: "Import this file? {added} new mapping(s), {updated} updated — {total} total after import. Nothing is saved until you confirm.",
    mappings_import_error_save: "Import failed: ",
    th_ha_path: "HA path", th_git_path: "Git path", th_direction: "Direction", th_manage: "Manage",
    mapping_edit: "Edit", mapping_delete: "Delete", mapping_normalize: "Make identical",
    mapping_compare_force: "Compare & force a version",
    mapping_preview: "Preview the difference",
    mapping_restore_backup: "Restore a previous backup",
    mapping_modal_add: "Add a mapping", mapping_modal_edit: "Edit mapping",
    field_id: "Identifier", field_kind: "Type", field_direction: "Direction",
    field_ha_path: "Home Assistant path", field_git_path: "Git path",
    field_protect: "🔒 Protect this file — never let Deploy/Git overwrite it",
    mapping_protect_warning: "⚠️ This is one of Home Assistant's own core config files. Leaving it unprotected is strongly discouraged: a failed deployment here could break your whole Home Assistant instance.",
    field_normalize_line_endings: "🔧 Auto-fix line endings on Push instead of blocking",
    field_create_if_missing: "📄 Create the file/directory now on any side where it doesn't exist yet",
    confirm_unprotect_core: "You are about to save \"{target}\" WITHOUT protection, on a core Home Assistant config file. This is strongly discouraged — a bad deployment could break your Home Assistant instance. Continue anyway?",
    kind_file: "File", kind_directory: "Directory (comparison only)",
    dir_git_to_ha: "Git -> Home Assistant",
    dir_ha_to_git: "Home Assistant -> Git",
    dir_bidirectional: "Bidirectional",
    browse: "Browse…", browse_title: "Browse",
    browse_select_here: "Select this folder",
    cancel: "Cancel", save: "Save",
    mapping_error_incomplete: "Please fill in the identifier, the HA path and the Git path.",
    mapping_confirm_delete: 'Delete mapping "{id}"?\n\nThis only removes it from the configuration — no file is touched.',
    mapping_error_delete: "Could not delete: ",
    mapping_error_direction: "Could not change direction: ",
    restart_overlay_text: "Applying your change — the add-on is restarting…",
    restart_overlay_timeout: "This is taking longer than expected. Try reloading this page in a moment.",
  },
  fr: {
    title: "Homelab Git Management",
    subtitle: "Contrôle Git ↔ Home Assistant",
    refresh: "Actualiser", refreshing: "Actualisation...",
    buy_me_a_beer: "☕ M'offrir une bière",
    readonly_banner: "Le bouton Actualiser met à jour le clone Git (lecture GitHub uniquement). Le bouton Déployer, visible uniquement sur les éléments autorisés, écrit sur Home Assistant après confirmation explicite.",
    card_app: "Application", card_app_meta: "version runtime",
    card_managed: "Éléments gérés", card_managed_meta: "mappings",
    card_identical: "Identiques", card_identical_meta: "égalité stricte",
    card_different: "Différents", card_different_meta: "contenu réel",
    card_candidates: "À déployer", card_candidates_meta: "candidats autorisés",
    table_title: "Éléments gérés", loading: "Chargement…",
    th_element: "Élément", th_comparison: "Comparaison",
    state_identical: "IDENTIQUE", state_equivalent: "ÉQUIVALENT", state_different: "DIFFÉRENT",
    state_missing_ha: "ABSENT HA", state_missing_git: "ABSENT GIT",
    state_missing_both: "ABSENT DES DEUX", state_error: "ERREUR", state_unknown: "INCONNU",
    protected: "PROTÉGÉ",
    action_deploy: "Déployer",
    action_push: "Envoyer", action_resolve: "Résoudre",
    pushing: "Envoi...",
    state_conflict: "CONFLIT", state_external: "CHANGEMENT EXTERNE",
    error_load: "Impossible de charger l'état : ", error_refresh: "Échec de l'actualisation Git : ",
    error_deploy: "Échec du déploiement : ",
    error_push: "Échec de l'envoi : ", error_acknowledge: "Impossible d'acquitter : ",
    error_normalize: "Impossible de rendre identique : ",
    deploying: "Déploiement...",
    confirm_deploy: 'Déployer « {target} » de GitHub vers Home Assistant ?\n\nUne sauvegarde du fichier actuel sera créée avant l\'écriture. En cas d\'échec de la vérification, un rollback automatique restaure l\'ancien contenu.',
    confirm_push: 'Envoyer « {target} » de Home Assistant vers GitHub ?\n\nCela crée un vrai commit sur votre dépôt.',
    confirm_force_push: 'Forcer l\'envoi de « {target} » ? Ceci écrase ce qui est actuellement sur GitHub par la version Home Assistant — le changement côté GitHub affiché dans le diff sera perdu.',
    confirm_force_deploy: 'Forcer le déploiement de « {target} » ? Ceci écrase le fichier Home Assistant actuel par la version GitHub — le changement côté Home Assistant affiché dans le diff sera perdu.',
    confirm_keep_git: 'Accepter le contenu GitHub actuel comme nouvelle référence pour « {target} » ? Rien n\'est envoyé et Home Assistant n\'est pas modifié — si ça diffère toujours ensuite, un envoi normal redevient possible.',
    confirm_normalize: 'Rendre « {target} » identique octet par octet ? Ceci prend la version Home Assistant exacte (y compris ses fins de ligne) et l\'écrit dans Git comme un commit purement formel — aucun contenu ne change, seule la représentation en octets change.',
    keys_panel_title: "🔑 Clés de déploiement (lecture + écriture)",
    write_key_intro: "Nécessaire uniquement si vous configurez un mapping avec direction: ha_to_git. C'est une clé séparée de celle ci-dessus — la clé en lecture seule n'obtient jamais d'accès écriture, et cette clé reste inactive tant que vous ne l'ajoutez pas vous-même sur GitHub, cette fois en autorisant l'écriture.",
    setup_write_key_label: "Clé publique en écriture :",
    conflict_modal_title: "Résoudre",
    conflict_ha_date: "Home Assistant, dernière modification :",
    conflict_git_date: "Git, dernier commit :",
    conflict_keep_git: "Garder la version Git",
    conflict_force_git: "Forcer le déploiement Git → Home Assistant",
    conflict_force_ha: "Forcer l'envoi Home Assistant → Git",
    conflict_explain_conflict: "Home Assistant et Git ont tous les deux changé indépendamment depuis la dernière synchro. Regardez la différence ci-dessous, puis choisissez quelle version garder.",
    conflict_explain_external: "Git a changé en dehors de cet add-on (probablement modifié directement sur GitHub) depuis la dernière synchro. Envoyer maintenant écraserait silencieusement ce changement.",
    conflict_explain_manual: "Comparaison manuelle : regardez la différence ci-dessous, puis choisissez quelle version garder. Utile quand un Push ou un Déploiement normal est bloqué pour une autre raison (par exemple une incohérence de fins de ligne) et que vous voulez quand même forcer un côté.",
    conflict_explain_preview: "Aperçu : regardez la différence ci-dessous avant de décider. Confirmer redemande quand même la confirmation normale et ne saute aucune vérification de sécurité.",
    backups_modal_title: "Restaurer une sauvegarde précédente",
    backups_explain: "Chaque entrée ci-dessous est un instantané de ce fichier pris automatiquement juste avant qu'un déploiement précédent ne l'écrase. Restaurer l'écrit maintenant sur Home Assistant — le contenu actuel est d'abord lui-même sauvegardé, donc c'est toujours réversible aussi.",
    backups_none: "Aucune sauvegarde pour l'instant pour ce mapping.",
    backups_restore: "Restaurer",
    backups_error_load: "Impossible de charger les sauvegardes : ",
    backups_error_restore: "Impossible de restaurer : ",
    confirm_restore_backup: 'Restaurer cette sauvegarde de « {target} » ? Le contenu Home Assistant actuel sera d\'abord sauvegardé, puis écrasé par cette version plus ancienne.',
    conflict_diff_loading: "Chargement de la différence…",
    conflict_diff_binary: "Le contenu de ce fichier ne peut pas être affiché en texte.",
    conflict_diff_too_large: "Ce fichier est trop volumineux pour être prévisualisé ici.",
    conflict_diff_error: "Impossible de charger la différence : ",
    conflict_diff_none: "Aucune différence textuelle à afficher.",
    plan_add: "+ AJOUT", plan_update: "~ MODIF.", plan_delete: "− SUPPR.",
    plan_section_git_to_ha: "Git → Home Assistant (Déployer)",
    plan_section_ha_to_git: "Home Assistant → Git (Pousser)",
    setup_title: "Configuration initiale",
    setup_step1: "Copiez la clé publique ci-dessous.",
    setup_step2: "Sur GitHub, ouvrez les Settings DU DÉPÔT lui-même -> Deploy keys -> Add deploy key (pas les paramètres de votre compte personnel).",
    setup_step3: 'Collez la clé, laissez "Allow write access" décoché, et enregistrez.',
    setup_step4: "Revenez ici, ouvrez l'onglet Configuration de cette Application, renseignez github_repository (owner/repository) et github_branch, puis redémarrez-la.",
    setup_key_label: "Clé publique :",
    setup_repo_label: "Dépôt configuré :",
    setup_branch_label: "Branche configurée :",
    copy_key: "Copier", copied_key: "Copié !",
    setup_none: "non défini",
    setup_open_config: "Ouvrir l'onglet Configuration →",
    mappings_add: "+ Ajouter un mapping",
    mappings_export: "Exporter les mappings (Excel)", mappings_import: "Importer des mappings (Excel)",
    mappings_export_error: "Impossible de générer le fichier Excel : ",
    mappings_import_error_parse: "Ce fichier n'est pas un classeur Excel (.xlsx) valide.",
    mappings_import_error_resolve: "Import impossible :\n",
    mappings_import_error_empty: "Ce fichier ne contient aucune ligne de mapping à importer.",
    mappings_import_confirm: "Importer ce fichier ? {added} nouveau(x) mapping(s), {updated} mis à jour — {total} au total après import. Rien n'est enregistré tant que vous ne confirmez pas.",
    mappings_import_error_save: "Échec de l'import : ",
    th_ha_path: "Chemin HA", th_git_path: "Chemin Git", th_direction: "Direction", th_manage: "Gérer",
    mapping_edit: "Modifier", mapping_delete: "Supprimer", mapping_normalize: "Rendre identique",
    mapping_compare_force: "Comparer et forcer une version",
    mapping_preview: "Aperçu de la différence",
    mapping_restore_backup: "Restaurer une sauvegarde précédente",
    mapping_modal_add: "Ajouter un mapping", mapping_modal_edit: "Modifier le mapping",
    field_id: "Identifiant", field_kind: "Type", field_direction: "Direction",
    field_ha_path: "Chemin Home Assistant", field_git_path: "Chemin Git",
    field_protect: "🔒 Protéger ce fichier — ne jamais laisser Déployer/Git l'écraser",
    mapping_protect_warning: "⚠️ Ceci est l'un des fichiers de configuration essentiels de Home Assistant. Le laisser sans protection est fortement déconseillé : un déploiement raté ici pourrait casser toute votre instance Home Assistant.",
    field_normalize_line_endings: "🔧 Corriger auto. les fins de ligne au Push au lieu de bloquer",
    field_create_if_missing: "📄 Créer le fichier/dossier maintenant du côté où il n'existe pas encore",
    confirm_unprotect_core: "Vous êtes sur le point d'enregistrer « {target} » SANS protection, sur un fichier de configuration essentiel de Home Assistant. C'est fortement déconseillé — un mauvais déploiement pourrait casser votre instance Home Assistant. Continuer quand même ?",
    kind_file: "Fichier", kind_directory: "Dossier (comparaison uniquement)",
    dir_git_to_ha: "Git -> Home Assistant",
    dir_ha_to_git: "Home Assistant -> Git",
    dir_bidirectional: "Bidirectionnel",
    browse: "Parcourir…", browse_title: "Parcourir",
    browse_select_here: "Choisir ce dossier",
    cancel: "Annuler", save: "Enregistrer",
    mapping_error_incomplete: "Merci de renseigner l'identifiant, le chemin HA et le chemin Git.",
    mapping_confirm_delete: 'Supprimer le mapping « {id} » ?\n\nCela retire uniquement la configuration — aucun fichier n\'est touché.',
    mapping_error_delete: "Impossible de supprimer : ",
    mapping_error_direction: "Impossible de changer la direction : ",
    restart_overlay_text: "Application du changement — l'add-on redémarre…",
    restart_overlay_timeout: "C'est plus long que prévu. Essaie de recharger cette page dans un instant.",
  },
};

let lang = localStorage.getItem("homelab-git-management-lang");
if (lang !== "en" && lang !== "fr") {
  lang = navigator.language && navigator.language.toLowerCase().startsWith("fr") ? "fr" : "en";
}

function t(key) { return STRINGS[lang][key] || key; }

function applyTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.getElementById("lang-toggle").textContent = lang === "en" ? "FR" : "EN";
  document.getElementById("refresh").textContent = t("refresh");
  document.title = t("title");
}

function switchLang() {
  lang = lang === "en" ? "fr" : "en";
  try { localStorage.setItem("homelab-git-management-lang", lang); } catch (e) {}
  applyTranslations();
  chargerEtatSiConfigure();
}

const el = (id) => document.getElementById(id);

let mappingsActuels = [];
let mappingEnEdition = null;
let browseRacine = null;
let browseCheminCourant = "";

function badge(texte, classe) {
  const span = document.createElement("span");
  span.className = `badge ${classe}`;
  span.textContent = texte;
  return span;
}

function badgeEtat(etat) {
  const map = {
    identical: [t("state_identical"), "identical"],
    equivalent: [t("state_equivalent"), "equivalent"],
    different: [t("state_different"), "different"],
    missing_ha: [t("state_missing_ha"), "different"],
    missing_git: [t("state_missing_git"), "blocked"],
    missing_both: [t("state_missing_both"), "blocked"],
    error: [t("state_error"), "blocked"],
  };
  const [label, cls] = map[etat] || [etat || t("state_unknown"), "neutral"];
  return badge(label, cls);
}

function cheminBaseIngress() {
  return window.location.pathname.endsWith("/") ? window.location.pathname : `${window.location.pathname}/`;
}

let menuKebabOuvert = null;

function fermerMenuKebab() {
  if (menuKebabOuvert) {
    menuKebabOuvert.hidden = true;
    menuKebabOuvert = null;
  }
}

document.addEventListener("click", (evenement) => {
  if (menuKebabOuvert && !menuKebabOuvert.parentElement.contains(evenement.target)) {
    fermerMenuKebab();
  }
});

function construireMenuKebab(fichier) {
  const enveloppe = document.createElement("div");
  enveloppe.className = "kebab-wrapper";

  const boutonKebab = document.createElement("button");
  boutonKebab.type = "button";
  boutonKebab.className = "kebab-btn";
  boutonKebab.textContent = "⋮";
  boutonKebab.setAttribute("aria-label", t("th_manage"));

  const menu = document.createElement("div");
  menu.className = "kebab-menu";
  menu.hidden = true;

  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.textContent = t("mapping_edit");
  editBtn.addEventListener("click", () => { fermerMenuKebab(); ouvrirModalMapping(fichier); });
  menu.appendChild(editBtn);

  if (fichier.normalizable_now) {
    const normalizeBtn = document.createElement("button");
    normalizeBtn.type = "button";
    normalizeBtn.textContent = t("mapping_normalize");
    normalizeBtn.addEventListener("click", () => { fermerMenuKebab(); normaliserElement(fichier.id); });
    menu.appendChild(normalizeBtn);
  }

  if (fichier.manually_resolvable_now) {
    const compareBtn = document.createElement("button");
    compareBtn.type = "button";
    compareBtn.textContent = t("mapping_compare_force");
    compareBtn.addEventListener("click", () => { fermerMenuKebab(); ouvrirConflitModal(fichier); });
    menu.appendChild(compareBtn);
  }

  if (fichier.previewable_now) {
    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.textContent = t("mapping_preview");
    previewBtn.addEventListener("click", () => { fermerMenuKebab(); ouvrirConflitModal(fichier); });
    menu.appendChild(previewBtn);
  }

  if (fichier.restaurable_now) {
    const restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.textContent = t("mapping_restore_backup");
    restoreBtn.addEventListener("click", () => { fermerMenuKebab(); ouvrirSauvegardesModal(fichier); });
    menu.appendChild(restoreBtn);
  }

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "danger";
  delBtn.textContent = t("mapping_delete");
  delBtn.addEventListener("click", () => { fermerMenuKebab(); supprimerMapping(fichier.id); });
  menu.appendChild(delBtn);

  boutonKebab.addEventListener("click", (evenement) => {
    evenement.stopPropagation();
    const etaitOuvert = menuKebabOuvert === menu;
    fermerMenuKebab();
    if (!etaitOuvert) {
      menu.hidden = false;
      menuKebabOuvert = menu;
    }
  });

  enveloppe.appendChild(boutonKebab);
  enveloppe.appendChild(menu);

  return enveloppe;
}

function afficherEtat(donnees) {
  el("version").textContent = donnees.application_version;
  el("managed").textContent = donnees.summary.managed;
  el("identical").textContent = donnees.summary.identical;
  el("different").textContent = donnees.summary.different;
  el("candidates").textContent = donnees.summary.deployable_now;

  const commit = donnees.git.head ? donnees.git.head.slice(0, 12) : "?";
  const clean = donnees.git.clean ? "clean" : "modified";
  el("status-line").textContent = `${donnees.git.branch || "?"} @ ${commit} · ${clean} · ${donnees.generated_at}`;

  const tbody = el("rows");
  tbody.replaceChildren();

  for (const fichier of donnees.elements) {
    const tr = document.createElement("tr");

    const idCell = document.createElement("td");
    const icone = document.createElement("span");
    icone.textContent = fichier.kind === "directory" ? "📁 " : "📄 ";
    idCell.appendChild(icone);
    const code = document.createElement("code");
    code.textContent = fichier.id;
    idCell.appendChild(code);
    tr.appendChild(idCell);

    const haCell = document.createElement("td");
    haCell.textContent = fichier.ha_path;
    tr.appendChild(haCell);

    const gitCell = document.createElement("td");
    gitCell.textContent = fichier.git_path;
    tr.appendChild(gitCell);

    const dirCell = document.createElement("td");
    const dirSelect = document.createElement("select");
    dirSelect.className = "direction-select";
    for (const valeur of ["git_to_ha", "ha_to_git", "bidirectional"]) {
      const option = document.createElement("option");
      option.value = valeur;
      option.textContent = t(`dir_${valeur}`);
      if (valeur === fichier.direction) option.selected = true;
      dirSelect.appendChild(option);
    }
    dirSelect.addEventListener("change", () =>
      changerDirection(fichier.id, dirSelect.value, dirSelect, fichier.direction)
    );
    dirCell.appendChild(dirSelect);
    tr.appendChild(dirCell);

    const cmpCell = document.createElement("td");
    cmpCell.appendChild(badgeEtat(fichier.state));
    if (fichier.protected) {
      const verrou = document.createElement("span");
      verrou.textContent = " 🔒";
      verrou.title = t("protected");
      cmpCell.appendChild(verrou);
    }
    if (fichier.sync_status === "conflict") {
      cmpCell.appendChild(badge(t("state_conflict"), "blocked"));
    } else if (fichier.sync_status === "external") {
      cmpCell.appendChild(badge(t("state_external"), "blocked"));
    }
    tr.appendChild(cmpCell);

    const gererCell = document.createElement("td");

    if (fichier.deployable_now) {
      const deployBtn = document.createElement("button");
      deployBtn.className = "action-deploy";
      deployBtn.textContent = t("action_deploy");
      deployBtn.addEventListener("click", () => deployerElement(fichier.id, deployBtn));
      gererCell.appendChild(deployBtn);
    }

    if (fichier.pushable_now) {
      const pushBtn = document.createElement("button");
      pushBtn.className = "action-push";
      pushBtn.textContent = t("action_push");
      pushBtn.addEventListener("click", () => pousserElement(fichier.id, pushBtn));
      gererCell.appendChild(pushBtn);
    }

    if (fichier.sync_status === "conflict" || fichier.sync_status === "external") {
      const resolveBtn = document.createElement("button");
      resolveBtn.className = "action-resolve";
      resolveBtn.textContent = t("action_resolve");
      resolveBtn.addEventListener("click", () => ouvrirConflitModal(fichier));
      gererCell.appendChild(resolveBtn);
    }

    gererCell.appendChild(construireMenuKebab(fichier));

    tr.appendChild(gererCell);

    tbody.appendChild(tr);
  }

  mappingsActuels = donnees.elements;
}

function copierMappingsPourEnvoi() {
  return mappingsActuels.map((mapping) => ({
    id: mapping.id, kind: mapping.kind, direction: mapping.direction,
    ha_path: mapping.ha_path, git_path: mapping.git_path,
    protect_from_git: !!mapping.protected,
    normalize_line_endings: !!mapping.normalize_line_endings,
  }));
}

// Home Assistant's own core config files. Used only to suggest a sensible
// default for the "protect this file" checkbox and to show the strong
// warning below — the real, authoritative protection decision is always
// whatever gestionnaire.py reads from the mapping's own protect_from_git
// field (or its own fallback for the same four files if that field was
// never set at all).
const CORE_HA_PATHS = [
  "/config/configuration.yaml", "/config/scripts.yaml",
  "/config/automations.yaml", "/config/scenes.yaml",
];

function estCheminEssentiel(chemin) {
  return CORE_HA_PATHS.includes((chemin || "").trim());
}

function appliquerDefautProtection() {
  if (mappingEnEdition) return; // never override an existing mapping's own stored choice
  const direction = el("mapping-direction").value;
  if (direction !== "git_to_ha" && direction !== "bidirectional") return;
  el("mapping-protect").checked = estCheminEssentiel(el("mapping-ha-path").value);
}

function mettreAJourAvertissementProtection() {
  const direction = el("mapping-direction").value;
  const directionConcernee = direction === "git_to_ha" || direction === "bidirectional";
  const estEssentiel = estCheminEssentiel(el("mapping-ha-path").value);

  el("mapping-protect-row").hidden = !directionConcernee;

  const avertissement = el("mapping-protect-warning");

  if (directionConcernee && estEssentiel && !el("mapping-protect").checked) {
    avertissement.textContent = t("mapping_protect_warning");
    avertissement.style.display = "block";
  } else {
    avertissement.style.display = "none";
  }
}

function mettreAJourVisibiliteNormalisation() {
  const direction = el("mapping-direction").value;
  el("mapping-normalize-row").hidden = direction !== "ha_to_git" && direction !== "bidirectional";
}

function ouvrirModalMapping(mapping) {
  mappingEnEdition = mapping ? mapping.id : null;

  const erreur = el("mapping-error");
  erreur.style.display = "none";

  el("mapping-modal-title").textContent = mapping ? t("mapping_modal_edit") : t("mapping_modal_add");

  el("mapping-id").value = mapping ? mapping.id : "";
  el("mapping-id").disabled = !!mapping;
  el("mapping-kind").value = mapping ? mapping.kind : "file";
  el("mapping-direction").value = mapping ? mapping.direction : "git_to_ha";
  el("mapping-ha-path").value = mapping ? mapping.ha_path : "";
  el("mapping-git-path").value = mapping ? mapping.git_path : "";
  el("mapping-protect").checked = mapping ? !!mapping.protected : false;
  el("mapping-normalize-line-endings").checked = mapping ? !!mapping.normalize_line_endings : false;
  // A one-time action, never a stored mapping property — always starts
  // unchecked, whether adding a new mapping or editing an existing one.
  el("mapping-create-if-missing").checked = false;

  mettreAJourAvertissementProtection();
  mettreAJourVisibiliteNormalisation();

  el("mapping-modal").hidden = false;
}

function fermerModalMapping() {
  el("mapping-modal").hidden = true;
}

async function attendreRedemarrage() {
  el("restart-overlay").hidden = false;

  // Give the container a moment to actually go down first, so an early
  // poll doesn't just hit the still-shutting-down process and report
  // "back" prematurely.
  await new Promise((resolve) => setTimeout(resolve, 1500));

  let revenu = false;

  for (let tentative = 0; tentative < 40; tentative++) {
    try {
      const reponse = await fetch(`${cheminBaseIngress()}api/setup`, { cache: "no-store" });
      if (reponse.ok) { revenu = true; break; }
    } catch (exception) {
      // Expected while the container is restarting — keep polling.
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  const overlayTexte = el("restart-overlay").querySelector("p");

  if (!revenu) {
    overlayTexte.textContent = t("restart_overlay_timeout");
    return;
  }

  el("restart-overlay").hidden = true;
  chargerEtatSiConfigure();
}

async function sauvegarderMapping() {
  const erreur = el("mapping-error");
  erreur.style.display = "none";

  const id = el("mapping-id").value.trim();
  const kind = el("mapping-kind").value;
  const direction = el("mapping-direction").value;
  const haPath = el("mapping-ha-path").value.trim();
  const gitPath = el("mapping-git-path").value.trim();

  if (!id || !haPath || !gitPath) {
    erreur.textContent = t("mapping_error_incomplete");
    erreur.style.display = "block";
    return;
  }

  const protectFromGit = el("mapping-protect").checked;
  const normalizeLineEndings = el("mapping-normalize-line-endings").checked;
  const createIfMissing = el("mapping-create-if-missing").checked;
  const directionConcernee = direction === "git_to_ha" || direction === "bidirectional";

  if (directionConcernee && estCheminEssentiel(haPath) && !protectFromGit) {
    if (!window.confirm(t("confirm_unprotect_core").replace("{target}", id))) return;
  }

  const nouveauMapping = {
    id, kind, direction, ha_path: haPath, git_path: gitPath,
    protect_from_git: protectFromGit, normalize_line_endings: normalizeLineEndings,
  };

  // A one-time instruction for this save only — never persisted as a
  // mapping property, see gerer_sauvegarde_mappings() server-side.
  if (createIfMissing) {
    nouveauMapping.create_if_missing = true;
  }
  const listeExistante = copierMappingsPourEnvoi();

  const nouvelleListe = mappingEnEdition
    ? listeExistante.map((mapping) => (mapping.id === mappingEnEdition ? nouveauMapping : mapping))
    : [...listeExistante, nouveauMapping];

  const boutonSave = el("mapping-save");
  boutonSave.disabled = true;

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: nouvelleListe }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    fermerModalMapping();

    if (donnees.restarting) {
      attendreRedemarrage();
    } else {
      afficherEtat(donnees);
    }
  } catch (exception) {
    erreur.textContent = exception.message;
    erreur.style.display = "block";
  } finally {
    boutonSave.disabled = false;
  }
}

async function changerDirection(id, nouvelleDirection, selectEl, directionPrecedente) {
  const erreur = el("error");
  erreur.style.display = "none";
  selectEl.disabled = true;

  const listeExistante = copierMappingsPourEnvoi();
  const nouvelleListe = listeExistante.map((mapping) =>
    mapping.id === id ? { ...mapping, direction: nouvelleDirection } : mapping
  );

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: nouvelleListe }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    if (donnees.restarting) {
      attendreRedemarrage();
    } else {
      afficherEtat(donnees);
    }
  } catch (exception) {
    erreur.textContent = t("mapping_error_direction") + exception.message;
    erreur.style.display = "block";
    selectEl.value = directionPrecedente;
    selectEl.disabled = false;
  }
}

async function supprimerMapping(id) {
  if (!window.confirm(t("mapping_confirm_delete").replace("{id}", id))) return;

  const nouvelleListe = copierMappingsPourEnvoi().filter((mapping) => mapping.id !== id);
  const erreur = el("error");

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: nouvelleListe }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    erreur.style.display = "none";

    if (donnees.restarting) {
      attendreRedemarrage();
    } else {
      afficherEtat(donnees);
    }
  } catch (exception) {
    erreur.textContent = t("mapping_error_delete") + exception.message;
    erreur.style.display = "block";
  }
}

async function exporterMappings() {
  const erreur = el("error");
  erreur.style.display = "none";

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings/export.xlsx`, { cache: "no-store" });
    if (!reponse.ok) throw new Error(`HTTP ${reponse.status}`);
    const blob = await reponse.blob();
    const url = URL.createObjectURL(blob);

    const lien = document.createElement("a");
    lien.href = url;
    lien.download = "mappings.xlsx";
    document.body.appendChild(lien);
    lien.click();
    document.body.removeChild(lien);
    URL.revokeObjectURL(url);
  } catch (exception) {
    erreur.textContent = t("mappings_export_error") + exception.message;
    erreur.style.display = "block";
  }
}

function declencherImportMappings() {
  // Reset first so re-selecting the exact same file still fires "change".
  el("import-mappings-input").value = "";
  el("import-mappings-input").click();
}

async function importerMappingsDepuisFichier(fichier) {
  const erreur = el("error");
  erreur.style.display = "none";

  // The workbook only tells us which HA/Git file or directory NAME was
  // picked for each row (or a literal path typed in place of a name);
  // resolving that against the real HA/Git trees can only happen
  // server-side, so this first call is read-only — nothing is saved yet.
  let resolus;
  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings/import-excel-preview`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/octet-stream" },
      body: await fichier.arrayBuffer(),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    resolus = donnees.resolved;
  } catch (exception) {
    erreur.textContent = t("mappings_import_error_resolve") + exception.message;
    erreur.style.display = "block";
    return;
  }

  if (!Array.isArray(resolus) || resolus.length === 0) {
    erreur.textContent = t("mappings_import_error_empty");
    erreur.style.display = "block";
    return;
  }

  const nouvelleListe = copierMappingsPourEnvoi();
  let ajoutes = 0;
  let misAJour = 0;

  for (const mapping of resolus) {
    const indexExistant = nouvelleListe.findIndex((existant) => existant.id === mapping.id);

    if (indexExistant === -1) {
      nouvelleListe.push(mapping);
      ajoutes++;
    } else {
      nouvelleListe[indexExistant] = mapping;
      misAJour++;
    }
  }

  const resume = t("mappings_import_confirm")
    .replace("{added}", ajoutes).replace("{updated}", misAJour).replace("{total}", nouvelleListe.length);

  if (!window.confirm(resume)) return;

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/mappings`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: nouvelleListe }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    if (donnees.restarting) {
      attendreRedemarrage();
    } else {
      afficherEtat(donnees);
    }
  } catch (exception) {
    erreur.textContent = t("mappings_import_error_save") + exception.message;
    erreur.style.display = "block";
  }
}

let browseADejaRepliCetteOuverture = false;

function ouvrirBrowse(racine, cheminActuel) {
  browseRacine = racine;
  browseADejaRepliCetteOuverture = false;
  browseCheminCourant = cheminActuel && cheminActuel.trim()
    ? cheminActuel.trim()
    : (racine === "ha" ? "/config" : "");

  el("browse-modal").hidden = false;
  chargerBrowse();
}

function fermerBrowse() {
  el("browse-modal").hidden = true;
}

function cheminParentSimple(chemin, racine) {
  if (racine === "ha") {
    const parties = chemin.replace(/^\/config\/?/, "").split("/").filter(Boolean);
    parties.pop();
    return parties.length ? `/config/${parties.join("/")}` : "/config";
  }
  const parties = chemin.split("/").filter(Boolean);
  parties.pop();
  return parties.join("/");
}

async function chargerBrowse() {
  const erreur = el("browse-error");
  erreur.style.display = "none";
  el("browse-list").replaceChildren();

  try {
    const parametres = new URLSearchParams({ root: browseRacine, path: browseCheminCourant });
    const reponse = await fetch(`${cheminBaseIngress()}api/browse?${parametres}`, { cache: "no-store" });
    const donnees = await reponse.json();

    if (!reponse.ok || !donnees.ok) {
      // Reopening Browse on a field that already holds a file path (not
      // a directory) used to just show an error. Back up to the parent
      // directory once instead, like a normal file picker would.
      if (!browseADejaRepliCetteOuverture && browseCheminCourant) {
        browseADejaRepliCetteOuverture = true;
        browseCheminCourant = cheminParentSimple(browseCheminCourant, browseRacine);
        return chargerBrowse();
      }
      throw new Error(donnees.error || `HTTP ${reponse.status}`);
    }

    browseCheminCourant = donnees.path;
    el("browse-breadcrumb").textContent = donnees.path || (browseRacine === "git" ? "/" : "/config");

    const liste = el("browse-list");

    if (donnees.parent !== null) {
      const li = document.createElement("li");
      li.className = "browse-entry browse-up";
      li.textContent = "⬆ …";
      li.addEventListener("click", () => { browseCheminCourant = donnees.parent; chargerBrowse(); });
      liste.appendChild(li);
    }

    for (const entree of donnees.entries) {
      const li = document.createElement("li");
      li.className = `browse-entry browse-${entree.kind}`;
      li.textContent = (entree.kind === "directory" ? "📁 " : "📄 ") + entree.name;

      if (entree.kind === "directory") {
        li.addEventListener("click", () => { browseCheminCourant = entree.path; chargerBrowse(); });
      } else {
        li.addEventListener("click", () => selectionnerCheminBrowse(entree.path));
      }

      liste.appendChild(li);
    }
  } catch (exception) {
    erreur.textContent = exception.message;
    erreur.style.display = "block";
  }
}

function selectionnerCheminBrowse(chemin) {
  if (browseRacine === "ha") {
    el("mapping-ha-path").value = chemin;
    appliquerDefautProtection();
    mettreAJourAvertissementProtection();
  } else {
    el("mapping-git-path").value = chemin;
  }
  fermerBrowse();
}

async function chargerSetup() {
  const reponse = await fetch(`${cheminBaseIngress()}api/setup`, { cache: "no-store" });
  return reponse.json();
}

async function chargerEtatSiConfigure() {
  const erreur = el("error");
  try {
    const setup = await chargerSetup();

    if (!setup.configured) {
      el("keys-panel").hidden = true;
      el("setup-panel").hidden = false;
      el("dashboard").hidden = true;
      el("refresh").hidden = true;
      el("setup-key").textContent = setup.public_key || "—";
      el("setup-repo").textContent = setup.github_repository || t("setup_none");
      el("setup-branch").textContent = setup.github_branch || t("setup_none");

      const lienConfig = el("setup-config-link");
      if (setup.config_url) {
        lienConfig.href = setup.config_url;
        lienConfig.hidden = false;
      } else {
        lienConfig.hidden = true;
      }

      // Only set when a repository IS configured but this boot couldn't
      // finish setting it up (GitHub unreachable, a stale/mismatched
      // clone, ...) — the specific reason from run.sh, so this doesn't
      // read as a silent, unexplained "not configured yet" to someone
      // who already filled everything in correctly.
      if (setup.warning) {
        erreur.textContent = setup.warning;
        erreur.style.display = "block";
      } else {
        erreur.style.display = "none";
      }

      return;
    }

    // Once configured, both keys are just reference info someone needs
    // rarely (adding a second mapping's write key, re-pointing at a new
    // repo) — collapsed by default instead of permanently taking up the
    // top of the dashboard.
    el("setup-key-collapsed").textContent = setup.public_key || "—";
    el("setup-key-write").textContent = setup.public_key_write || "—";
    el("keys-panel").hidden = false;

    el("setup-panel").hidden = true;
    el("dashboard").hidden = false;
    el("refresh").hidden = false;

    const reponse = await fetch(`${cheminBaseIngress()}api/status`, { cache: "no-store" });
    const donnees = await reponse.json();

    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    erreur.style.display = "none";
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_load") + exception.message;
    erreur.style.display = "block";
  }
}

async function actualiserGit() {
  const erreur = el("error");
  const bouton = el("refresh");
  erreur.style.display = "none";
  bouton.disabled = true;
  const libelle = bouton.textContent;
  bouton.textContent = t("refreshing");
  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/refresh`, { method: "POST", cache: "no-store" });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_refresh") + exception.message;
    erreur.style.display = "block";
  } finally {
    bouton.disabled = false;
    bouton.textContent = libelle;
  }
}

async function deployerElement(cible, bouton, depuisApercu = false) {
  if (!depuisApercu) {
    const fichier = mappingsActuels.find((mapping) => mapping.id === cible);
    if (fichier && fichier.kind === "directory") {
      ouvrirConflitModal(fichier);
      return;
    }
  }

  if (!window.confirm(t("confirm_deploy").replace("{target}", cible))) return;

  const erreur = el("error");
  erreur.style.display = "none";
  if (bouton) { bouton.disabled = true; }
  const libelle = bouton ? bouton.textContent : null;
  if (bouton) { bouton.textContent = t("deploying"); }

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/deploy`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: cible, sens: "git_to_ha" }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_deploy") + exception.message;
    erreur.style.display = "block";
    if (bouton) { bouton.disabled = false; bouton.textContent = libelle; }
  }
}

async function pousserElement(cible, bouton, depuisApercu = false) {
  if (!depuisApercu) {
    const fichier = mappingsActuels.find((mapping) => mapping.id === cible);
    if (fichier && fichier.kind === "directory") {
      ouvrirConflitModal(fichier);
      return;
    }
  }

  if (!window.confirm(t("confirm_push").replace("{target}", cible))) return;

  const erreur = el("error");
  erreur.style.display = "none";
  if (bouton) { bouton.disabled = true; }
  const libelle = bouton ? bouton.textContent : null;
  if (bouton) { bouton.textContent = t("pushing"); }

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/deploy`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: cible, force: false, sens: "ha_to_git" }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_push") + exception.message;
    erreur.style.display = "block";
    if (bouton) { bouton.disabled = false; bouton.textContent = libelle; }
  }
}

async function normaliserElement(cible) {
  if (!window.confirm(t("confirm_normalize").replace("{target}", cible))) return;

  const erreur = el("error");
  erreur.style.display = "none";

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/normalize-git`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: cible }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_normalize") + exception.message;
    erreur.style.display = "block";
  }
}

let conflictCibleCourante = null;
let conflictDirectionCourante = null;
let conflictModeCourant = "conflict";

function construireLigneDiff(ligne) {
  const span = document.createElement("span");
  if (ligne.startsWith("+") && !ligne.startsWith("+++")) {
    span.className = "diff-add";
  } else if (ligne.startsWith("-") && !ligne.startsWith("---")) {
    span.className = "diff-remove";
  } else if (ligne.startsWith("@@")) {
    span.className = "diff-hunk";
  }
  span.textContent = ligne;
  return span;
}

async function chargerPlanRepertoire(cible, sens) {
  const reponse = await fetch(
    `${cheminBaseIngress()}api/directory-plan?target=${encodeURIComponent(cible)}&sens=${sens}`,
    { cache: "no-store" }
  );
  const donnees = await reponse.json();
  if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
  return donnees.plan;
}

function construireLignePlan(entree) {
  const div = document.createElement("div");
  const libelleAction =
    entree.action === "add" ? t("plan_add")
    : entree.action === "update" ? t("plan_update")
    : t("plan_delete");
  div.className = `plan-line plan-${entree.action}`;
  div.textContent = `${libelleAction}  ${entree.relatif}`;
  return div;
}

function construirePlanListe(plan) {
  const conteneur = document.createElement("div");

  if (plan.length === 0) {
    conteneur.textContent = t("conflict_diff_none");
    return conteneur;
  }

  for (const entree of plan) {
    conteneur.appendChild(construireLignePlan(entree));
  }

  return conteneur;
}

function construireSectionPlan(titre, plan) {
  const section = document.createElement("div");
  const titreEl = document.createElement("div");
  titreEl.className = "plan-section-title";
  titreEl.textContent = titre;
  section.appendChild(titreEl);
  section.appendChild(construirePlanListe(plan));
  return section;
}

// Directories skip the two-column text diff entirely (it does not make
// sense for a whole tree) and show the exact add/update/delete plan
// planifier_repertoire() will apply instead — the same plan the action
// itself recomputes fresh right before running, never a stale preview.
// Only a bidirectional mapping being force-resolved ("conflict"/"manual"
// mode) ever has two directions genuinely on the table, so that is the
// only case showing both plans; a plain one-direction mapping (git_to_ha
// or ha_to_git only) always shows just its own direction's plan, "conflict"/
// "external" acknowledge-only mode included — there is no other
// direction it could ever act on.
async function chargerApercuRepertoire(fichier, diffEl, erreur) {
  try {
    if (fichier.direction === "bidirectional" && conflictModeCourant !== "preview") {
      const [planGit, planHa] = await Promise.all([
        chargerPlanRepertoire(fichier.id, "git_to_ha"),
        chargerPlanRepertoire(fichier.id, "ha_to_git"),
      ]);
      diffEl.replaceChildren();
      diffEl.appendChild(construireSectionPlan(t("plan_section_git_to_ha"), planGit));
      diffEl.appendChild(construireSectionPlan(t("plan_section_ha_to_git"), planHa));
    } else {
      const sens = fichier.direction === "git_to_ha" ? "git_to_ha" : "ha_to_git";
      const plan = await chargerPlanRepertoire(fichier.id, sens);
      diffEl.replaceChildren();
      diffEl.appendChild(construirePlanListe(plan));
    }
  } catch (exception) {
    diffEl.textContent = "";
    erreur.textContent = t("conflict_diff_error") + exception.message;
    erreur.style.display = "block";
  }
}

async function ouvrirConflitModal(fichier) {
  conflictCibleCourante = fichier.id;
  conflictDirectionCourante = fichier.direction;

  conflictModeCourant =
    fichier.sync_status === "conflict" || fichier.sync_status === "external" ? "conflict"
    : fichier.direction === "bidirectional" ? "manual"
    : "preview";

  const erreur = el("conflict-error");
  erreur.style.display = "none";

  el("conflict-explain").textContent =
    conflictModeCourant === "conflict"
      ? (fichier.sync_status === "conflict" ? t("conflict_explain_conflict") : t("conflict_explain_external"))
    : conflictModeCourant === "manual" ? t("conflict_explain_manual")
    : t("conflict_explain_preview");

  if (conflictModeCourant === "preview") {
    el("conflict-keep-git").textContent =
      fichier.direction === "git_to_ha" ? t("action_deploy") : t("action_push");
    el("conflict-force-ha").hidden = true;
  } else {
    el("conflict-keep-git").textContent =
      fichier.direction === "bidirectional" ? t("conflict_force_git") : t("conflict_keep_git");
    el("conflict-force-ha").hidden = false;
  }

  el("conflict-ha-date").textContent = "…";
  el("conflict-git-date").textContent = "…";
  el("conflict-dates-row").hidden = fichier.kind === "directory";

  const diffEl = el("conflict-diff");
  diffEl.replaceChildren();
  diffEl.textContent = t("conflict_diff_loading");

  el("conflict-modal").hidden = false;

  if (fichier.kind === "directory") {
    await chargerApercuRepertoire(fichier, diffEl, erreur);
    return;
  }

  try {
    const reponse = await fetch(
      `${cheminBaseIngress()}api/diff?target=${encodeURIComponent(fichier.id)}`,
      { cache: "no-store" }
    );
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    el("conflict-ha-date").textContent = donnees.ha_modified_at || "—";
    el("conflict-git-date").textContent = donnees.git_committed_at || "—";

    diffEl.replaceChildren();

    if (donnees.binary) {
      diffEl.textContent = t("conflict_diff_binary");
    } else if (donnees.too_large) {
      diffEl.textContent = t("conflict_diff_too_large");
    } else if (!donnees.diff) {
      diffEl.textContent = t("conflict_diff_none");
    } else {
      for (const ligne of donnees.diff.split("\n").filter((l) => l.length > 0)) {
        diffEl.appendChild(construireLigneDiff(ligne));
      }
    }
  } catch (exception) {
    diffEl.textContent = "";
    erreur.textContent = t("conflict_diff_error") + exception.message;
    erreur.style.display = "block";
  }
}

function fermerConflitModal() {
  el("conflict-modal").hidden = true;
  el("conflict-force-ha").hidden = false;
  conflictCibleCourante = null;
  conflictDirectionCourante = null;
  conflictModeCourant = "conflict";
}

async function resoudreForcer() {
  if (!conflictCibleCourante) return;
  if (!window.confirm(t("confirm_force_push").replace("{target}", conflictCibleCourante))) return;

  const erreur = el("conflict-error");

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/deploy`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: conflictCibleCourante, force: true, sens: "ha_to_git" }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    fermerConflitModal();
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_push") + exception.message;
    erreur.style.display = "block";
  }
}

async function resoudreForcerDeploy() {
  if (!conflictCibleCourante) return;
  if (!window.confirm(t("confirm_force_deploy").replace("{target}", conflictCibleCourante))) return;

  const erreur = el("conflict-error");

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/deploy`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: conflictCibleCourante, force: true, sens: "git_to_ha" }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    fermerConflitModal();
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_deploy") + exception.message;
    erreur.style.display = "block";
  }
}

async function resoudreAcquitter() {
  if (!conflictCibleCourante) return;
  if (!window.confirm(t("confirm_keep_git").replace("{target}", conflictCibleCourante))) return;

  const erreur = el("conflict-error");

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/acknowledge-git`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: conflictCibleCourante }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    fermerConflitModal();
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_acknowledge") + exception.message;
    erreur.style.display = "block";
  }
}

function resoudrePreviewAction() {
  if (!conflictCibleCourante) return;
  const cible = conflictCibleCourante;
  const direction = conflictDirectionCourante;
  fermerConflitModal();
  if (direction === "git_to_ha") {
    deployerElement(cible, null, true);
  } else {
    pousserElement(cible, null, true);
  }
}

function resoudreVersGit() {
  if (conflictModeCourant === "preview") {
    resoudrePreviewAction();
  } else if (conflictDirectionCourante === "bidirectional") {
    resoudreForcerDeploy();
  } else {
    resoudreAcquitter();
  }
}

let backupsCibleCourante = null;

function formaterTailleOctets(octets) {
  if (octets < 1024) return `${octets} B`;
  if (octets < 1024 * 1024) return `${(octets / 1024).toFixed(1)} KB`;
  return `${(octets / (1024 * 1024)).toFixed(1)} MB`;
}

async function ouvrirSauvegardesModal(fichier) {
  backupsCibleCourante = fichier.id;

  const erreur = el("backups-error");
  erreur.style.display = "none";

  const liste = el("backups-list");
  liste.replaceChildren();
  liste.textContent = t("loading");

  el("backups-modal").hidden = false;

  try {
    const reponse = await fetch(
      `${cheminBaseIngress()}api/backups?target=${encodeURIComponent(fichier.id)}`,
      { cache: "no-store" }
    );
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

    liste.replaceChildren();

    if (!donnees.backups.length) {
      liste.textContent = t("backups_none");
      return;
    }

    for (const sauvegarde of donnees.backups) {
      const ligne = document.createElement("div");
      ligne.className = "backup-row";

      const meta = document.createElement("div");
      const date = document.createElement("div");
      date.className = "backup-meta";
      date.textContent = sauvegarde.created_at;
      const taille = document.createElement("div");
      taille.className = "backup-size";
      taille.textContent = formaterTailleOctets(sauvegarde.size);
      meta.appendChild(date);
      meta.appendChild(taille);

      const boutonRestaurer = document.createElement("button");
      boutonRestaurer.type = "button";
      boutonRestaurer.textContent = t("backups_restore");
      boutonRestaurer.addEventListener("click", () => restaurerSauvegarde(sauvegarde.name, boutonRestaurer));

      ligne.appendChild(meta);
      ligne.appendChild(boutonRestaurer);
      liste.appendChild(ligne);
    }
  } catch (exception) {
    liste.textContent = "";
    erreur.textContent = t("backups_error_load") + exception.message;
    erreur.style.display = "block";
  }
}

function fermerSauvegardesModal() {
  el("backups-modal").hidden = true;
  backupsCibleCourante = null;
}

async function restaurerSauvegarde(nomSauvegarde, bouton) {
  if (!backupsCibleCourante) return;
  if (!window.confirm(t("confirm_restore_backup").replace("{target}", backupsCibleCourante))) return;

  const erreur = el("backups-error");
  erreur.style.display = "none";
  bouton.disabled = true;

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/restore-backup`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: backupsCibleCourante, backup: nomSauvegarde }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    fermerSauvegardesModal();
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("backups_error_restore") + exception.message;
    erreur.style.display = "block";
    bouton.disabled = false;
  }
}

async function copierCle() {
  const bouton = el("copy-key");
  const cle = el("setup-key").textContent;

  if (!cle || cle === "—") return;

  try {
    await navigator.clipboard.writeText(cle);
  } catch (exception) {
    return;
  }

  const libelle = bouton.textContent;
  bouton.textContent = t("copied_key");
  bouton.disabled = true;

  setTimeout(() => {
    bouton.textContent = libelle;
    bouton.disabled = false;
  }, 1500);
}

async function copierCleCollapsed() {
  const bouton = el("copy-key-collapsed");
  const cle = el("setup-key-collapsed").textContent;

  if (!cle || cle === "—") return;

  try {
    await navigator.clipboard.writeText(cle);
  } catch (exception) {
    return;
  }

  const libelle = bouton.textContent;
  bouton.textContent = t("copied_key");
  bouton.disabled = true;

  setTimeout(() => {
    bouton.textContent = libelle;
    bouton.disabled = false;
  }, 1500);
}

async function copierCleEcriture() {
  const bouton = el("copy-key-write");
  const cle = el("setup-key-write").textContent;

  if (!cle || cle === "—") return;

  try {
    await navigator.clipboard.writeText(cle);
  } catch (exception) {
    return;
  }

  const libelle = bouton.textContent;
  bouton.textContent = t("copied_key");
  bouton.disabled = true;

  setTimeout(() => {
    bouton.textContent = libelle;
    bouton.disabled = false;
  }, 1500);
}

el("lang-toggle").addEventListener("click", switchLang);
el("refresh").addEventListener("click", actualiserGit);
el("copy-key").addEventListener("click", copierCle);
el("copy-key-collapsed").addEventListener("click", copierCleCollapsed);
el("copy-key-write").addEventListener("click", copierCleEcriture);

el("conflict-modal-close").addEventListener("click", fermerConflitModal);
el("conflict-cancel").addEventListener("click", fermerConflitModal);
el("conflict-force-ha").addEventListener("click", resoudreForcer);
el("conflict-keep-git").addEventListener("click", resoudreVersGit);
el("backups-modal-close").addEventListener("click", fermerSauvegardesModal);
el("backups-cancel").addEventListener("click", fermerSauvegardesModal);

el("add-mapping").addEventListener("click", () => ouvrirModalMapping(null));
el("export-mappings").addEventListener("click", exporterMappings);
el("import-mappings").addEventListener("click", declencherImportMappings);
el("import-mappings-input").addEventListener("change", (event) => {
  const fichier = event.target.files && event.target.files[0];
  if (fichier) importerMappingsDepuisFichier(fichier);
});
el("mapping-modal-close").addEventListener("click", fermerModalMapping);
el("mapping-cancel").addEventListener("click", fermerModalMapping);
el("mapping-save").addEventListener("click", sauvegarderMapping);

el("mapping-direction").addEventListener("change", () => {
  appliquerDefautProtection();
  mettreAJourAvertissementProtection();
  mettreAJourVisibiliteNormalisation();
});
el("mapping-ha-path").addEventListener("input", () => {
  appliquerDefautProtection();
  mettreAJourAvertissementProtection();
});
el("mapping-protect").addEventListener("change", mettreAJourAvertissementProtection);

document.querySelectorAll(".browse-btn").forEach((bouton) => {
  bouton.addEventListener("click", () => {
    const racine = bouton.getAttribute("data-root");
    const champ = racine === "ha" ? el("mapping-ha-path") : el("mapping-git-path");
    ouvrirBrowse(racine, champ.value);
  });
});

el("browse-modal-close").addEventListener("click", fermerBrowse);
el("browse-select-here").addEventListener("click", () => selectionnerCheminBrowse(browseCheminCourant));

applyTranslations();
chargerEtatSiConfigure();
setInterval(chargerEtatSiConfigure, 5000);
</script>
</body>
</html>
"""


###############################################################################
# OPTIONS / SETUP
###############################################################################

def lire_options() -> dict:

    try:
        with OPTIONS_PATH.open("r", encoding="utf-8") as fichier:
            donnees = json.load(fichier)
        return donnees if isinstance(donnees, dict) else {}
    except Exception:
        return {}


# Cached after the first successful lookup: the add-on's own slug never
# changes during the container's lifetime, and this must not be re-fetched
# from Supervisor on every 5-second setup poll.
_slug_propre_cache: str | None = None
_slug_propre_echec = False


def obtenir_slug_propre() -> str | None:
    """Read-only lookup of this add-on's own Supervisor slug (includes the
    repository hash prefix, e.g. "08626b84_homelab_git_management"), used
    only to build a convenience link to the native Configuration tab. Never
    writes anything; any failure just means the link is not shown."""

    global _slug_propre_cache, _slug_propre_echec

    if _slug_propre_cache is not None:
        return _slug_propre_cache

    if _slug_propre_echec:
        return None

    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        _slug_propre_echec = True
        return None

    requete = urllib.request.Request(
        f"{SUPERVISOR_API}/addons/self/info",
        headers={"Authorization": f"Bearer {token}"},
    )

    try:
        with urllib.request.urlopen(requete, timeout=5) as reponse:
            donnees = json.loads(reponse.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        _slug_propre_echec = True
        return None

    slug = donnees.get("data", {}).get("slug") if isinstance(donnees, dict) else None

    if not isinstance(slug, str) or not slug:
        _slug_propre_echec = True
        return None

    _slug_propre_cache = slug

    return slug


def construire_setup() -> dict:

    options = lire_options()
    repo = options.get("github_repository") or ""

    public_key = None

    if SSH_PUBLIC_KEY.is_file():
        try:
            public_key = SSH_PUBLIC_KEY.read_text(encoding="utf-8").strip()
        except Exception:
            public_key = None

    public_key_write = None

    if SSH_PUBLIC_KEY_WRITE.is_file():
        try:
            public_key_write = SSH_PUBLIC_KEY_WRITE.read_text(encoding="utf-8").strip()
        except Exception:
            public_key_write = None

    # A repository *option* being set is not the same as a real, usable
    # clone existing: run.sh deliberately does not crash the whole add-on
    # (and with it, this very screen) just because GitHub wasn't
    # reachable yet at boot — most commonly because github_repository was
    # filled in before the Deploy Key was ever added on GitHub. In that
    # case the clone was skipped for this boot, so falling back to the
    # same setup screen shown before any repository is configured at all
    # (key + the repository/branch already saved) is the only way this
    # ever becomes recoverable from the UI instead of requiring a manual
    # edit of options.json.
    clone_pret = (GIT_ROOT / ".git").is_dir()

    config_url = None

    if not repo:

        slug = obtenir_slug_propre()

        if slug:
            config_url = f"/config/app/{slug}/config"

    return {
        "ok": True,
        "configured": bool(repo) and clone_pret,
        "github_repository": repo,
        "github_branch": options.get("github_branch") or "main",
        "public_key": public_key,
        "public_key_write": public_key_write,
        "config_url": config_url,
        # Set by run.sh (read once at process start, fixed for this
        # boot) only when a repository IS configured but this boot
        # couldn't finish setting it up — GitHub unreachable, a stale or
        # mismatched local clone, a failed clone, or the engine's own
        # initial validation failing. None of those are ever fatal to
        # this add-on's own startup any more; this is the specific
        # reason, shown directly on the setup screen instead of leaving
        # someone who already did everything right guessing why they're
        # still looking at it.
        "warning": os.environ.get("GIT_SETUP_WARNING") or None,
    }


###############################################################################
# DIRECTORY BROWSING (read-only)
#
# Powers the "Browse" picker in the mapping form: lists the direct
# children of a directory, always confined to the given root (HA config
# mount or the local Git clone). Never reads file contents, never
# follows symlinks, never lists outside its root.
###############################################################################

def verifier_chemin_resolu(chemin: Path, racine: Path) -> None:

    racine_resolue = racine.resolve()
    chemin_resolu = chemin.resolve()

    chemin_resolu.relative_to(racine_resolue)


def lister_repertoire(racine: Path, sous_chemin_relatif: str) -> dict:
    """Lists the direct children of `racine / sous_chemin_relatif`.

    `sous_chemin_relatif` must already be a plain relative path (no
    leading '/', no '..'); the two HTTP-facing prefixes (ha_path's
    "/config/..." form, git_path's plain relative form) are converted to
    that shape by the caller before this ever runs.
    """

    relatif = PurePosixPath(sous_chemin_relatif) if sous_chemin_relatif else PurePosixPath()

    if relatif.is_absolute():
        raise ValueError("path must be relative")

    if ".." in relatif.parts:
        raise ValueError("path must not contain '..'")

    cible = racine.joinpath(*relatif.parts) if relatif.parts else racine

    verifier_chemin_resolu(cible, racine)

    if cible.is_symlink():
        raise ValueError("symlinks are not browsable")

    if not cible.is_dir():
        raise ValueError("not a directory")

    if not os.access(cible, os.R_OK | os.X_OK):
        raise ValueError("directory not accessible")

    entrees = []

    with os.scandir(cible) as parcours:
        for entree in parcours:

            if entree.name.startswith("."):
                continue

            if entree.name in NOMS_IGNORES_NAVIGATION:
                continue

            if entree.is_symlink():
                continue

            chemin_enfant = (relatif / entree.name).as_posix()

            if entree.is_dir(follow_symlinks=False):
                entrees.append({"name": entree.name, "kind": "directory", "path": chemin_enfant})
            elif entree.is_file(follow_symlinks=False):
                entrees.append({"name": entree.name, "kind": "file", "path": chemin_enfant})

    entrees.sort(key=lambda entree: (entree["kind"] != "directory", entree["name"].lower()))

    if relatif.parts:
        parent = relatif.parent.as_posix()
        parent = "" if parent == "." else parent
    else:
        parent = None

    return {
        "path": relatif.as_posix() if relatif.parts else "",
        "parent": parent,
        "entries": entrees,
    }


def ha_path_vers_relatif(ha_path: str) -> str:
    """Converts a "/config/..." style path (the shape stored in a
    mapping's ha_path) to a plain path relative to HA_ROOT."""

    chemin = (ha_path or "").strip().lstrip("/")

    if chemin == "config":
        return ""

    if chemin.startswith("config/"):
        return chemin[len("config/"):]

    return chemin


def relatif_vers_ha_path(relatif: str) -> str:
    return f"/config/{relatif}" if relatif else "/config"


def gerer_browse(handler: "InterfaceHandler") -> None:

    requete = urllib.parse.urlsplit(handler.path)
    parametres = urllib.parse.parse_qs(requete.query)

    racine_nom = (parametres.get("root") or [""])[0]
    chemin_brut = (parametres.get("path") or [""])[0]

    if racine_nom == "ha":
        racine = HA_ROOT
        chemin_relatif = ha_path_vers_relatif(chemin_brut)
    elif racine_nom == "git":
        racine = GIT_ROOT
        chemin_relatif = (chemin_brut or "").strip().lstrip("/")
    else:
        handler.repondre_json(400, {"ok": False, "error": "invalid root (expected 'ha' or 'git')"})
        return

    try:
        resultat = lister_repertoire(racine, chemin_relatif)
    except (ValueError, OSError) as exc:
        handler.repondre_json(400, {"ok": False, "error": str(exc)})
        return

    def formater(relatif: str) -> str:
        return relatif_vers_ha_path(relatif) if racine_nom == "ha" else relatif

    handler.repondre_json(200, {
        "ok": True,
        "root": racine_nom,
        "path": formater(resultat["path"]),
        "parent": None if resultat["parent"] is None else formater(resultat["parent"]),
        "entries": [
            {**entree, "path": formater(entree["path"])}
            for entree in resultat["entries"]
        ],
    })


###############################################################################
# DIFF (ha_to_git conflict resolution)
#
# The one endpoint that ever sends real file content to the browser,
# strictly scoped to a single narrow purpose: helping a user decide which
# side to keep when a ha_to_git mapping shows "conflict" or "external".
# Refuses anything else (wrong direction, wrong kind) rather than
# becoming a general "view file content" feature, skips binary content,
# and caps how much it will ever read per side.
###############################################################################

def formater_date_commit_git(git_path_relatif: str) -> str | None:

    resultat = subprocess.run(
        ["git", "-C", str(GIT_ROOT), "log", "-1", "--format=%cI", "--", git_path_relatif],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )

    if resultat.returncode != 0:
        return None

    return resultat.stdout.strip() or None


def gerer_diff(handler: "InterfaceHandler") -> None:

    requete = urllib.parse.urlsplit(handler.path)
    parametres = urllib.parse.parse_qs(requete.query)
    cible = (parametres.get("target") or [""])[0]

    if not cible:
        handler.repondre_json(400, {"ok": False, "error": "missing target"})
        return

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        handler.repondre_json(503, {"ok": False, "error": erreur_chargement})
        return

    elements_par_id = {element["id"]: element for element in module.elements}
    element = elements_par_id.get(cible)

    if element is None:
        handler.repondre_json(404, {"ok": False, "error": "unmanaged element"})
        return

    if element["kind"] != "file":
        handler.repondre_json(
            400,
            {"ok": False, "error": "diff is only available for file mappings"},
        )
        return

    try:
        chemin_ha = module.convertir_chemin_ha(element["ha_path"])
        chemin_git = module.convertir_chemin_git(element["git_path"])
        module.verifier_chemin_resolu(chemin_ha, module.HA_ROOT, "/homeassistant")
        module.verifier_chemin_resolu(chemin_git, module.GIT_ROOT, "/data/repository")
    except ValueError as exc:
        handler.repondre_json(400, {"ok": False, "error": str(exc)})
        return

    ha_existe = chemin_ha.exists() and chemin_ha.is_file()
    git_existe = chemin_git.exists() and chemin_git.is_file()

    reponse = {
        "ok": True,
        "sync_status": module.etats_synchro.get(
            cible, "conflict" if element["direction"] == "bidirectional" else "clean"
        ),
        "ha_modified_at": (
            datetime.fromtimestamp(chemin_ha.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            if ha_existe else None
        ),
        "git_committed_at": (
            formater_date_commit_git(element["git_path"]) if git_existe else None
        ),
        "binary": False,
        "too_large": False,
        "diff": "",
    }

    if not ha_existe or not git_existe:
        handler.repondre_json(200, reponse)
        return

    if chemin_ha.stat().st_size > DIFF_MAX_BYTES or chemin_git.stat().st_size > DIFF_MAX_BYTES:
        reponse["too_large"] = True
        handler.repondre_json(200, reponse)
        return

    contenu_ha = chemin_ha.read_bytes()
    contenu_git = chemin_git.read_bytes()

    if not module.est_fichier_texte(chemin_ha, contenu_ha) or not module.est_fichier_texte(chemin_git, contenu_git):
        reponse["binary"] = True
        handler.repondre_json(200, reponse)
        return

    lignes_ha = contenu_ha.decode("utf-8", errors="replace").splitlines(keepends=True)
    lignes_git = contenu_git.decode("utf-8", errors="replace").splitlines(keepends=True)

    reponse["diff"] = "".join(difflib.unified_diff(
        lignes_git, lignes_ha,
        fromfile=f"git:{element['git_path']}",
        tofile=f"ha:{element['ha_path']}",
        n=3,
    ))

    handler.repondre_json(200, reponse)


def gerer_plan_repertoire(handler: "InterfaceHandler") -> None:
    """The directory-kind counterpart of gerer_diff(): instead of a
    two-column text diff (which does not make sense for a whole tree),
    returns the add/update/delete plan a directory Deploy, Push, or
    bidirectional force would apply — read-only, nothing here ever
    writes anything. `sens` picks which direction's plan: 'git_to_ha'
    (what Deploy, or the Git -> HA side of a conflict resolution, would
    do) or 'ha_to_git' (Push, or the other side)."""

    requete = urllib.parse.urlsplit(handler.path)
    parametres = urllib.parse.parse_qs(requete.query)
    cible = (parametres.get("target") or [""])[0]
    sens = (parametres.get("sens") or [""])[0]

    if not cible:
        handler.repondre_json(400, {"ok": False, "error": "missing target"})
        return

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        handler.repondre_json(503, {"ok": False, "error": erreur_chargement})
        return

    try:
        plan = module.obtenir_plan_repertoire(cible, sens)
    except ValueError as exc:
        handler.repondre_json(400, {"ok": False, "error": str(exc)})
        return

    handler.repondre_json(200, {"ok": True, "plan": plan})


###############################################################################
# BACKUPS (restore a previous local backup)
#
# Every git_to_ha deploy already backs up the Home Assistant content it
# is about to overwrite (see gestionnaire.creer_sauvegarde()). This just
# exposes that existing archive so a human can browse and restore from
# it, instead of it only ever being used internally for the automatic
# write-failure rollback.
###############################################################################

def gerer_liste_sauvegardes(handler: "InterfaceHandler") -> None:

    requete = urllib.parse.urlsplit(handler.path)
    parametres = urllib.parse.parse_qs(requete.query)
    cible = (parametres.get("target") or [""])[0]

    if not cible:
        handler.repondre_json(400, {"ok": False, "error": "missing target"})
        return

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        handler.repondre_json(503, {"ok": False, "error": erreur_chargement})
        return

    if cible not in {element["id"] for element in module.elements}:
        handler.repondre_json(404, {"ok": False, "error": "unmanaged element"})
        return

    handler.repondre_json(200, {"ok": True, "backups": module.lister_sauvegardes(cible)})


def declencher_restauration_sauvegarde(cible: object, nom_sauvegarde: object) -> tuple[bool, str | None]:

    if not isinstance(cible, str) or not cible:
        return False, "invalid target"

    if not isinstance(nom_sauvegarde, str) or not nom_sauvegarde:
        return False, "invalid backup name"

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        return False, erreur_chargement

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            module.restaurer_depuis_sauvegarde(cible, nom_sauvegarde)

    except SystemExit as exc:

        message = sortie_capturee.getvalue().strip()

        print(sortie_capturee.getvalue(), end="", flush=True)

        return False, message or f"restore refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"{type(exc).__name__}: {exc}"

    print(sortie_capturee.getvalue(), end="", flush=True)
    return True, None


###############################################################################
# MAPPINGS: WRITE
#
# The one place this add-on writes to its own configuration. Until now,
# interface.py only ever read /data/options.json — the user edited it
# exclusively through Home Assistant's native Configuration tab, and
# Supervisor wrote it. This adds a second, additional way to manage
# mappings (a picker inside our own page); the native tab keeps working
# exactly as before, nothing is removed.
#
# Security:
#   - every submitted mapping is re-validated with the engine's own
#     rules (gestionnaire.py's ID_PATTERN, ALLOWED_KINDS,
#     IMPLEMENTED_DIRECTIONS, convertir_chemin_ha/git,
#     verifier_chemin_resolu, est_chemin_protege) — nothing is accepted
#     here that the engine itself would refuse to load or deploy;
#   - the Supervisor call this uses (POST /addons/self/options) is
#     scoped to "self": it can only ever change this add-on's own
#     configuration, never another add-on's or Home Assistant's;
#   - the full current options object is sent back with only `mappings`
#     replaced — the Supervisor API validates everything against
#     config.yaml's schema in one pass, so a partial payload would not
#     be reliable.
###############################################################################

REQUEST_PAYLOAD_MAX_BYTES = 262144  # 256 KiB: generous, but bounded — shared
                                     # cap for every POST body this add-on
                                     # reads (mappings, deploy target).


def valider_mappings_proposes(module, mappings_proposes: list) -> list:
    """Re-validates a candidate mappings list with the engine's own
    rules before it is ever persisted. Raises ValueError (safe to show
    to the user) on the first problem found."""

    ids_vus: set[str] = set()
    resultat = []

    for entree in mappings_proposes:

        if not isinstance(entree, dict):
            raise ValueError("a mapping entry must be an object")

        element_id = entree.get("id")

        if not isinstance(element_id, str) or not module.ID_PATTERN.fullmatch(element_id or ""):
            raise ValueError(f"invalid id: {element_id!r}")

        if element_id in ids_vus:
            raise ValueError(f"duplicate id: {element_id}")

        ids_vus.add(element_id)

        kind = entree.get("kind") or "file"

        if kind not in module.ALLOWED_KINDS:
            raise ValueError(f"{element_id}: invalid kind: {kind}")

        direction = entree.get("direction")

        if direction not in module.IMPLEMENTED_DIRECTIONS:
            raise ValueError(
                f"{element_id}: direction '{direction}' is not supported yet "
                f"(implemented: {', '.join(sorted(module.IMPLEMENTED_DIRECTIONS))}) "
                "— this would stop the engine from starting at all if saved"
            )

        ha_path = entree.get("ha_path")
        git_path = entree.get("git_path")

        if not isinstance(ha_path, str) or not ha_path.strip():
            raise ValueError(f"{element_id}: invalid ha_path")

        if not isinstance(git_path, str) or not git_path.strip():
            raise ValueError(f"{element_id}: invalid git_path")

        chemin_ha = module.convertir_chemin_ha(ha_path)
        module.verifier_chemin_resolu(chemin_ha, module.HA_ROOT, "/homeassistant")

        chemin_git = module.convertir_chemin_git(git_path)
        module.verifier_chemin_resolu(chemin_git, module.GIT_ROOT, "/data/repository")

        protect_from_git = entree.get("protect_from_git")

        if protect_from_git is not None and not isinstance(protect_from_git, bool):
            raise ValueError(f"{element_id}: invalid protect_from_git (must be true or false)")

        normalize_line_endings = entree.get("normalize_line_endings")

        if normalize_line_endings is not None and not isinstance(normalize_line_endings, bool):
            raise ValueError(f"{element_id}: invalid normalize_line_endings (must be true or false)")

        # A mapping's ha_path/git_path are picked independently (two
        # separate browse dialogs), so nothing before this enforced they
        # actually describe "the same" thing. Catch the two most common
        # mistakes explicitly, for whichever side already exists on disk,
        # instead of letting them surface later as an opaque engine
        # startup failure.
        if chemin_ha.exists():
            if kind == "file" and chemin_ha.is_dir():
                raise ValueError(f"{element_id}: ha_path is a directory, but kind is 'file'")
            if kind == "directory" and not chemin_ha.is_dir():
                raise ValueError(f"{element_id}: ha_path is a file, but kind is 'directory'")

        if chemin_git.exists():
            if kind == "file" and chemin_git.is_dir():
                raise ValueError(f"{element_id}: git_path is a directory, but kind is 'file'")
            if kind == "directory" and not chemin_git.is_dir():
                raise ValueError(f"{element_id}: git_path is a file, but kind is 'directory'")

        if kind == "file":
            suffixe_ha = PurePosixPath(ha_path).suffix.lower()
            suffixe_git = PurePosixPath(git_path).suffix.lower()
            if suffixe_ha != suffixe_git:
                raise ValueError(
                    f"{element_id}: ha_path and git_path have different file "
                    f"extensions ({suffixe_ha or '(none)'} vs {suffixe_git or '(none)'}) "
                    "— they are almost certainly not meant to be the same file"
                )

        resultat.append({
            "id": element_id,
            "kind": kind,
            "ha_path": ha_path.strip(),
            "git_path": git_path.strip(),
            "direction": direction,
            "protect_from_git": protect_from_git,
            "normalize_line_endings": bool(normalize_line_endings),
        })

    return resultat


def ecrire_options_supervisor(champs: dict) -> tuple[bool, str | None]:
    """Writes to this add-on's own Supervisor-tracked options."""

    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return False, "SUPERVISOR_TOKEN not available"

    options_actuelles = lire_options()
    nouvelles_options = {**options_actuelles, **champs}

    corps = json.dumps({"options": nouvelles_options}).encode("utf-8")

    requete = urllib.request.Request(
        f"{SUPERVISOR_API}/addons/self/options",
        data=corps,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(requete, timeout=10) as reponse:
            reponse.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        return False, f"Supervisor refused the options update: {exc.code} {detail}".strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"cannot reach Supervisor: {exc}"

    return True, None


def redemarrer_soi_meme() -> None:
    """Restarts this add-on via the Supervisor API, scoped to "self" just
    like the options write above — it can only ever restart this add-on,
    never another one. Used only as a fallback when a saved mapping isn't
    visible yet: a manual restart from the Info tab was observed during
    testing to reliably make it appear, so this automates exactly that
    instead of leaving the user to do it by hand. Best-effort: any
    failure here is silent, since the caller has already told the
    browser a restart is starting and the browser's own retry loop is
    what actually determines when the add-on is back."""

    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return

    requete = urllib.request.Request(
        f"{SUPERVISOR_API}/addons/self/restart",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )

    try:
        urllib.request.urlopen(requete, timeout=10)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


###############################################################################
# HOME ASSISTANT CORE API (config check + notifications)
#
# Both reach Home Assistant Core itself, through the Supervisor proxy at
# http://supervisor/core/api/... — a different, wider grant
# (homeassistant_api: true) than the self-scoped Supervisor API above.
# Both are best-effort conveniences: neither one is ever allowed to turn
# an otherwise-successful write into a failure just because Home
# Assistant Core happened to be briefly unreachable (e.g. restarting).
###############################################################################

def verifier_config_ha() -> tuple[bool, str | None]:
    """Calls Home Assistant Core's own config-check endpoint
    (POST /api/config/core/check_config) right after a write into Home
    Assistant, to catch a configuration Core itself would refuse to load
    — before it ever gets the chance to. Returns (True, None) when valid
    *or* when the check could not be reached at all (nothing here should
    block a deploy whose bytes already wrote and verified correctly just
    because this optional extra check was unreachable); returns
    (False, message) only on a genuine "invalid" verdict from Core."""

    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return True, None

    requete = urllib.request.Request(
        f"{SUPERVISOR_API}/core/api/config/core/check_config",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            resultat = json.loads(reponse.read())
    except Exception as exc:
        print(
            f"[homelab-git-management] [interface] WARNING: could not reach "
            f"Home Assistant's config check: {exc}",
            flush=True,
        )
        return True, None

    if resultat.get("result") == "invalid":
        return False, resultat.get("errors") or "Home Assistant reports the configuration is invalid"

    return True, None


def envoyer_notification_ha(notification_id: str, titre: str, message: str) -> None:
    """Creates (or replaces, same notification_id) a Home Assistant
    persistent notification, so a push/deploy failure or an
    auto-rollback is visible from Home Assistant itself, not only from
    this add-on's own dashboard. Best-effort and silent on failure —
    never raises, never affects the outcome of the action it reports on,
    which has already happened by the time this is called."""

    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return

    corps = json.dumps({
        "notification_id": f"homelab_git_management_{notification_id}",
        "title": titre,
        "message": message,
    }).encode("utf-8")

    requete = urllib.request.Request(
        f"{SUPERVISOR_API}/core/api/services/persistent_notification/create",
        data=corps,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    try:
        urllib.request.urlopen(requete, timeout=10)
    except Exception as exc:
        print(
            f"[homelab-git-management] [interface] WARNING: could not create "
            f"Home Assistant notification: {exc}",
            flush=True,
        )


def mappings_correspondent(actuels: object, attendus: list) -> bool:
    """Structural comparison used only to detect whether the just-written
    mappings have become visible yet — not a validation function."""

    def normaliser(liste):
        if not isinstance(liste, list):
            return None
        return [
            (m.get("id"), m.get("kind") or "file", m.get("ha_path"), m.get("git_path"), m.get("direction"))
            for m in liste
            if isinstance(m, dict)
        ]

    return normaliser(actuels) == normaliser(attendus)


def attendre_options_persistees(mappings_attendus: list, delai_max: float = 3.0) -> bool:
    """Best-effort only: Supervisor's POST /addons/self/options can return
    success before the on-disk options.json this add-on reads is actually
    updated — observed during end-to-end testing, anywhere from under a
    second to (rarely) not until the add-on is restarted. This makes the
    common case feel instant by waiting briefly, but the caller must never
    treat a timeout here as a failure: Supervisor's own success response
    is the real confirmation, and the regular 5-second status poll picks
    up the change on its own once it lands, with no action needed."""

    intervalle = 0.25
    ecoule = 0.0

    while ecoule < delai_max:
        if mappings_correspondent(lire_options().get("mappings"), mappings_attendus):
            return True
        time.sleep(intervalle)
        ecoule += intervalle

    return mappings_correspondent(lire_options().get("mappings"), mappings_attendus)


###############################################################################
# BULK MAPPINGS EXPORT/IMPORT (Excel)
#
# Export produces an .xlsx workbook: one row per current mapping, plus a
# handful of blank template rows, with dropdown lists (native Excel data
# validation, no macro) for direction/kind/booleans and for the HA/Git
# file or directory *name* — built by scanning the real trees at export
# time. A dropdown only ever offers a bare name, never a full path: an
# existing mapping's row instead carries its real, already-unambiguous
# path directly (see construire_classeur_export), so re-importing an
# untouched row can never fail just because some unrelated file
# elsewhere happens to share its name.
#
# Import is a two-step, read-then-confirm flow, mirroring the previous
# JSON import's UX: gerer_import_excel_preview() only resolves each row's
# bare name against the current trees (or accepts a value containing "/"
# as an already-complete literal path) and reports the result — nothing
# is saved yet. Any name that resolves to zero or more-than-one match
# fails the whole import, in one message covering every offending row
# (checked before the browser is even asked to confirm anything). Only
# once the browser has merged the resolved rows into the current list
# and the user has confirmed does it call the existing, unrelated
# /api/mappings endpoint to actually save — identical validation and
# persistence path as every other way of editing mappings.
###############################################################################

def parcourir_tous_les_noms(racine: Path) -> list[tuple[str, str, str]]:
    """Recursively walks `racine`, applying the exact same exclusions as
    the directory browser (dotfiles/dotdirs, NOMS_IGNORES_NAVIGATION,
    never following or listing symlinks). Returns (name, relative_path,
    kind) for every file and directory found, relative_path being posix
    and relative to `racine` itself."""

    resultats: list[tuple[str, str, str]] = []

    def parcourir(dossier: Path, prefixe: PurePosixPath) -> None:

        try:
            with os.scandir(dossier) as parcours:
                entrees = list(parcours)
        except OSError:
            return

        for entree in entrees:

            if entree.name.startswith("."):
                continue

            if entree.name in NOMS_IGNORES_NAVIGATION:
                continue

            if entree.is_symlink():
                continue

            chemin_relatif = (prefixe / entree.name).as_posix()

            if entree.is_dir(follow_symlinks=False):
                resultats.append((entree.name, chemin_relatif, "directory"))
                parcourir(Path(entree.path), prefixe / entree.name)
            elif entree.is_file(follow_symlinks=False):
                resultats.append((entree.name, chemin_relatif, "file"))

    parcourir(racine, PurePosixPath())

    return resultats


def indexer_arborescence(racine: Path) -> dict[tuple[str, str], list[str]]:
    """Builds a (kind, name) -> [relative paths] lookup for `racine`, used
    to resolve a bare name typed or picked in an imported workbook. More
    than one entry for a given key means that name is ambiguous under
    this root."""

    index: dict[tuple[str, str], list[str]] = {}

    for nom, relatif, kind in parcourir_tous_les_noms(racine):
        index.setdefault((kind, nom), []).append(relatif)

    return index


def valeur_booleenne(valeur) -> bool:
    """Normalizes a workbook cell's value to a bool. Excel auto-recognizes
    a bare typed TRUE/FALSE as a real Boolean cell (not text), so this
    must accept both an actual bool and a "true"/"false" string."""

    if isinstance(valeur, bool):
        return valeur

    return str(valeur if valeur is not None else "").strip().lower() == "true"


def resoudre_ligne_excel(
    valeur, index_arborescence: dict[tuple[str, str], list[str]], kind: str, formater_resultat
) -> tuple[str | None, str | None]:
    """Resolves one ha_file/git_file cell to a final path, or returns
    (None, error_message). A value containing '/' is treated as an
    already-complete path and used exactly as typed — this is also how
    an existing mapping's own row round-trips safely (see
    construire_classeur_export). A bare name is looked up in
    `index_arborescence`; zero or more than one match is an error."""

    valeur = str(valeur if valeur is not None else "").strip()

    if not valeur:
        return None, "missing file/directory name"

    if "/" in valeur:
        return valeur, None

    correspondances = index_arborescence.get((kind, valeur), [])

    if not correspondances:
        return None, f"no {kind} named '{valeur}' found"

    if len(correspondances) > 1:
        return None, f"ambiguous {kind} name '{valeur}' — matches: {', '.join(sorted(correspondances))}"

    return formater_resultat(correspondances[0]), None


def construire_classeur_export() -> bytes:

    elements = construire_etat().get("elements", [])

    noms_ha = sorted({nom for nom, _, _ in parcourir_tous_les_noms(HA_ROOT)})[:EXCEL_DROPDOWN_MAX_ENTRIES]
    noms_git = sorted({nom for nom, _, _ in parcourir_tous_les_noms(GIT_ROOT)})[:EXCEL_DROPDOWN_MAX_ENTRIES]

    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Mappings"

    feuille.append([
        "id", "kind", "direction", "ha_file", "git_file",
        "protect_from_git", "normalize_line_endings", "create_if_missing",
    ])

    for element in elements:
        feuille.append([
            element["id"], element["kind"], element["direction"],
            element["ha_path"], element["git_path"],
            "true" if element.get("protected") else "false",
            "true" if element.get("normalize_line_endings") else "false",
            "false",
        ])

    premiere_ligne_vide = len(elements) + 2
    derniere_ligne = premiere_ligne_vide + EXCEL_TEMPLATE_BLANK_ROWS - 1

    feuille_listes = classeur.create_sheet("Lists")
    feuille_listes.sheet_state = "hidden"

    for index, nom in enumerate(noms_ha, start=1):
        feuille_listes.cell(row=index, column=1, value=nom)

    for index, nom in enumerate(noms_git, start=1):
        feuille_listes.cell(row=index, column=2, value=nom)

    def plage(colonne: str, taille: int) -> str:
        return f"=Lists!${colonne}$1:${colonne}${max(taille, 1)}"

    # (column index, formula1) — an inline quoted list for the small,
    # fixed-choice columns, a range reference into the hidden "Lists"
    # sheet for the two name columns. showDropDown is left unset/False on
    # purpose: in the underlying file format that is what actually shows
    # the in-cell dropdown arrow, and — just as importantly — leaves
    # Excel free to accept a value outside the list (a literal path)
    # without popping up a blocking error.
    validations = [
        (2, '"file,directory"'),
        (3, '"git_to_ha,ha_to_git,bidirectional"'),
        (4, plage("A", len(noms_ha))),
        (5, plage("B", len(noms_git))),
        (6, '"true,false"'),
        (7, '"true,false"'),
        (8, '"true,false"'),
    ]

    for colonne_index, formule in validations:
        validation = DataValidation(type="list", formula1=formule, allow_blank=True)
        colonne_lettre = get_column_letter(colonne_index)
        validation.add(f"{colonne_lettre}2:{colonne_lettre}{derniere_ligne}")
        feuille.add_data_validation(validation)

    for colonne_index, largeur in zip(range(1, 9), [18, 10, 14, 42, 42, 16, 20, 18]):
        feuille.column_dimensions[get_column_letter(colonne_index)].width = largeur

    tampon = io.BytesIO()
    classeur.save(tampon)

    return tampon.getvalue()


def gerer_export_excel(handler: "InterfaceHandler") -> None:

    try:
        contenu = construire_classeur_export()
    except Exception as exc:
        handler.repondre_json(500, {"ok": False, "error": f"could not build the export: {exc}"})
        return

    handler.repondre_telechargement(
        contenu, "mappings.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def gerer_import_excel_preview(handler: "InterfaceHandler") -> None:

    corps = handler.lire_corps_borne(EXCEL_UPLOAD_MAX_BYTES)

    if corps is None:
        return

    try:
        classeur = openpyxl.load_workbook(io.BytesIO(corps), data_only=True, read_only=True)
    except Exception:
        handler.repondre_json(400, {"ok": False, "error": "this file is not a valid Excel (.xlsx) workbook"})
        return

    feuille = classeur["Mappings"] if "Mappings" in classeur.sheetnames else classeur.worksheets[0]

    index_ha = indexer_arborescence(HA_ROOT)
    index_git = indexer_arborescence(GIT_ROOT)

    erreurs: list[str] = []
    resolus: list[dict] = []

    for numero, ligne in enumerate(feuille.iter_rows(min_row=2, values_only=True), start=2):

        if ligne is None or all(valeur is None or str(valeur).strip() == "" for valeur in ligne):
            continue  # a blank template row

        valeurs = list(ligne) + [None] * max(0, 8 - len(ligne))
        (
            id_brut, kind_brut, direction_brut, ha_file_brut, git_file_brut,
            protect_brut, normalize_brut, creer_brut,
        ) = valeurs[:8]

        id_element = str(id_brut if id_brut is not None else "").strip()
        kind = str(kind_brut if kind_brut is not None else "").strip() or "file"

        if not id_element:
            erreurs.append(f"row {numero}: missing id")
            continue

        ha_path, erreur_ha = resoudre_ligne_excel(ha_file_brut, index_ha, kind, relatif_vers_ha_path)

        if erreur_ha:
            erreurs.append(f"row {numero} ({id_element}): {erreur_ha}")

        git_path, erreur_git = resoudre_ligne_excel(git_file_brut, index_git, kind, lambda relatif: relatif)

        if erreur_git:
            erreurs.append(f"row {numero} ({id_element}): {erreur_git}")

        if erreur_ha or erreur_git:
            continue

        mapping_resolu = {
            "id": id_element,
            "kind": kind,
            "direction": str(direction_brut if direction_brut is not None else "").strip(),
            "ha_path": ha_path,
            "git_path": git_path,
            "protect_from_git": valeur_booleenne(protect_brut),
            "normalize_line_endings": valeur_booleenne(normalize_brut),
        }

        # A one-time instruction for the save that follows this preview,
        # never a stored mapping property — see
        # gerer_sauvegarde_mappings(). A bare name that resolved to
        # nothing would already have failed above as "not found"; a
        # literal path (typed instead of picked from the dropdown) is
        # exactly how a not-yet-existing target is named here, and
        # needs no special casing for that reason.
        if valeur_booleenne(creer_brut):
            mapping_resolu["create_if_missing"] = True

        resolus.append(mapping_resolu)

    if erreurs:
        handler.repondre_json(400, {"ok": False, "error": "\n".join(erreurs)})
        return

    handler.repondre_json(200, {"ok": True, "resolved": resolus})


def gerer_sauvegarde_mappings(handler: "InterfaceHandler") -> None:

    corps_requete = handler.lire_corps_borne(REQUEST_PAYLOAD_MAX_BYTES)

    if corps_requete is None:
        return

    try:
        charge = json.loads(corps_requete)
    except json.JSONDecodeError:
        handler.repondre_json(400, {"ok": False, "error": "invalid JSON"})
        return

    if not isinstance(charge, dict) or not isinstance(charge.get("mappings"), list):
        handler.repondre_json(400, {"ok": False, "error": "expected {\"mappings\": [...]}"})
        return

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        handler.repondre_json(503, {"ok": False, "error": erreur_chargement})
        return

    try:
        mappings_valides = valider_mappings_proposes(module, charge["mappings"])
    except ValueError as exc:
        handler.repondre_json(400, {"ok": False, "error": str(exc)})
        return

    # "create_if_missing" is a one-time instruction for this save only
    # (the mapping form's checkbox, or a bulk Excel import column) —
    # valider_mappings_proposes() already dropped it from
    # mappings_valides since it builds its own dict of known fields, so
    # it must be read from the raw, pre-validation payload instead.
    ids_a_creer = {
        entree.get("id") for entree in charge["mappings"]
        if isinstance(entree, dict) and entree.get("create_if_missing")
    }

    for mapping in mappings_valides:

        if mapping["id"] not in ids_a_creer:
            continue

        try:
            module.creer_elements_manquants(mapping)
        except RuntimeError as exc:
            handler.repondre_json(502, {"ok": False, "error": f"{mapping['id']}: {exc}"})
            return

    succes, erreur_ecriture = ecrire_options_supervisor({"mappings": mappings_valides})

    if not succes:
        handler.repondre_json(502, {"ok": False, "error": erreur_ecriture})
        return

    if attendre_options_persistees(mappings_valides, delai_max=1.5):

        print(
            f"[homelab-git-management] [interface] mappings updated via UI "
            f"({len(mappings_valides)} entries)",
            flush=True,
        )

        donnees = construire_etat()
        handler.repondre_json(200 if donnees.get("ok") else 503, donnees)
        return

    # Not visible yet through the fast path. Testing showed a plain
    # restart reliably makes a just-saved mapping appear (Supervisor's
    # write to options.json can apparently lag until the add-on is
    # restarted), so this automates exactly that instead of leaving the
    # user to do it by hand from the Info tab. The response is sent
    # *before* triggering the restart so the browser gets a clean
    # "restarting" signal first; the browser then polls on its own
    # until the add-on answers again.
    print(
        "[homelab-git-management] [interface] mappings accepted by Supervisor "
        "but not yet visible; restarting to apply them",
        flush=True,
    )
    handler.repondre_json(200, {"ok": True, "restarting": True})
    redemarrer_soi_meme()


###############################################################################
# GIT INFO
###############################################################################

def informations_git() -> dict:

    def lancer(*arguments: str) -> str | None:

        resultat = subprocess.run(
            ["git", "-C", str(GIT_ROOT), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )

        if resultat.returncode != 0:
            return None

        return resultat.stdout.strip()

    return {
        "head": lancer("rev-parse", "HEAD"),
        "branch": lancer("branch", "--show-current"),
        "clean": (
            lancer("status", "--porcelain", "--untracked-files=all") == ""
        ),
    }


###############################################################################
# ENGINE LOADING
###############################################################################

def charger_gestionnaire():

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            if "gestionnaire" in sys.modules:
                module = importlib.reload(sys.modules["gestionnaire"])
            else:
                module = importlib.import_module("gestionnaire")

        return module, None

    except SystemExit as exc:
        return None, f"engine stopped with code {exc.code}"

    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


###############################################################################
# GIT REFRESH
#
# The only entry point that triggers a real network fetch to GitHub.
# Calls gestionnaire.mettre_a_jour_clone() directly: no update logic is
# reimplemented here.
###############################################################################

def declencher_rafraichissement_git() -> tuple[bool, str | None]:

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        return False, erreur_chargement

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            module.mettre_a_jour_clone()

    except SystemExit as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"Git update refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"{type(exc).__name__}: {exc}"

    print(sortie_capturee.getvalue(), end="", flush=True)
    return True, None


###############################################################################
# DEPLOYMENT
#
# The only entry point that actually writes to Home Assistant. Calls
# gestionnaire.deployer_element(target, True) directly: every safety
# check lives in gestionnaire.py, never here. "True" is only ever passed
# because the browser button already required an explicit confirm().
###############################################################################

def declencher_deploiement(
    cible: object, forcer: object = False, sens: object = None
) -> tuple[bool, str | None]:

    if not isinstance(cible, str) or not cible:
        return False, "invalid target"

    if not isinstance(forcer, bool):
        return False, "invalid force flag"

    if sens is not None and sens not in {"git_to_ha", "ha_to_git"}:
        return False, "invalid sens"

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        return False, erreur_chargement

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            module.deployer_element(cible, True, forcer, sens)

    except SystemExit as exc:

        message = sortie_capturee.getvalue().strip()

        print(sortie_capturee.getvalue(), end="", flush=True)

        return False, message or f"deployment refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"{type(exc).__name__}: {exc}"

    print(sortie_capturee.getvalue(), end="", flush=True)

    elements_par_id = {element["id"]: element for element in module.elements}
    element = elements_par_id.get(cible)

    ecrit_vers_ha = element is not None and (
        element["direction"] == "git_to_ha"
        or (element["direction"] == "bidirectional" and sens == "git_to_ha")
    )

    if not ecrit_vers_ha:
        return True, None

    # This write already succeeded and verified byte-for-byte — from
    # here on, we're checking whether the *result* is a configuration
    # Home Assistant Core can actually load, and rolling back to the
    # backup this same deploy just made (see gestionnaire.deployer_fichier)
    # if not. A best-effort extra safety net, never a reason to fail a
    # deploy whose own write already succeeded, if this check itself
    # can't be reached.
    valide, erreur_config = verifier_config_ha()

    if valide:
        return True, None

    sauvegardes = module.lister_sauvegardes(cible)

    if not sauvegardes:
        envoyer_notification_ha(
            f"config_invalid_{cible}",
            "Homelab Git Management: invalid configuration",
            f'Deploying "{cible}" left Home Assistant\'s configuration invalid '
            f"({erreur_config}), and no backup exists to restore automatically — "
            "manual intervention is needed.",
        )
        return False, (
            f"deployed, but Home Assistant reports the new configuration is invalid "
            f"({erreur_config}) — no backup exists to auto-restore"
        )

    try:
        module.restaurer_sauvegarde_locale(element, sauvegardes[0]["name"])
    except Exception as exc:
        envoyer_notification_ha(
            f"config_invalid_{cible}",
            "Homelab Git Management: invalid configuration",
            f'Deploying "{cible}" left Home Assistant\'s configuration invalid '
            f"({erreur_config}), and the automatic rollback also failed ({exc}) — "
            "manual intervention is needed.",
        )
        return False, (
            f"deployed, but Home Assistant reports the new configuration is invalid "
            f"({erreur_config}), and the automatic rollback failed: {exc}"
        )

    envoyer_notification_ha(
        f"config_invalid_{cible}",
        "Homelab Git Management: deploy rolled back",
        f'Deploying "{cible}" left Home Assistant\'s configuration invalid '
        f"({erreur_config}). The previous version has been restored automatically.",
    )
    return False, (
        f"deploy rolled back: Home Assistant reports the new configuration is invalid "
        f"({erreur_config}); the previous version has been restored"
    )


###############################################################################
# ACKNOWLEDGE GIT (ha_to_git conflict/external resolution)
#
# Accepts the Git side's current content as the new reference point,
# without pushing anything and without touching Home Assistant. Calls
# gestionnaire.acquitter_git(target) directly — the same "single
# implementation of the rules" principle as every other write path here.
###############################################################################

def declencher_acquittement_git(cible: object) -> tuple[bool, str | None]:

    if not isinstance(cible, str) or not cible:
        return False, "invalid target"

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        return False, erreur_chargement

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            module.acquitter_git(cible)

    except SystemExit as exc:

        message = sortie_capturee.getvalue().strip()

        print(sortie_capturee.getvalue(), end="", flush=True)

        return False, message or f"acknowledge refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"{type(exc).__name__}: {exc}"

    print(sortie_capturee.getvalue(), end="", flush=True)
    return True, None


###############################################################################
# NORMALIZE TO IDENTICAL (equivalent -> identical, Home Assistant -> Git)
#
# Forces byte-for-byte identity for a mapping the comparison currently
# classifies "equivalent" (same content once formatting differences are
# normalized away, but not byte-identical). Calls
# gestionnaire.normaliser_git(target) directly, same pattern as every
# other write path here: the single implementation of the rules lives in
# gestionnaire.py.
###############################################################################

def declencher_normalisation_git(cible: object) -> tuple[bool, str | None]:

    if not isinstance(cible, str) or not cible:
        return False, "invalid target"

    module, erreur_chargement = charger_gestionnaire()

    if module is None:
        return False, erreur_chargement

    sortie_capturee = io.StringIO()

    try:

        with (
            contextlib.redirect_stdout(sortie_capturee),
            contextlib.redirect_stderr(sortie_capturee),
        ):

            module.normaliser_git(cible)

    except SystemExit as exc:

        message = sortie_capturee.getvalue().strip()

        print(sortie_capturee.getvalue(), end="", flush=True)

        return False, message or f"normalize refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        print(sortie_capturee.getvalue(), end="", flush=True)
        return False, f"{type(exc).__name__}: {exc}"

    print(sortie_capturee.getvalue(), end="", flush=True)
    return True, None


###############################################################################
# STATE FOR THE INTERFACE
###############################################################################

def construire_etat() -> dict:

    gestionnaire, erreur = charger_gestionnaire()

    if gestionnaire is None:
        return {
            "ok": False,
            "error": erreur,
            "application_version": APP_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    fichiers = []

    for element in gestionnaire.elements:

        element_id = element["id"]
        etat = gestionnaire.resultats_comparaison.get(element_id, "unknown")
        chemin_ha = gestionnaire.convertir_chemin_ha(element["ha_path"])
        protege = gestionnaire.est_chemin_protege(element, chemin_ha)
        direction = element["direction"]
        kind = element["kind"]

        sync_status = None
        deployable_now = False
        pushable_now = False

        if direction == "git_to_ha":

            deployable_now = (
                not protege
                and etat in {"different", "missing_ha"}
            )

        elif direction == "ha_to_git":

            sync_status = gestionnaire.etats_synchro.get(element_id, "clean")
            pushable_now = (
                sync_status == "clean"
                and etat in {"different", "missing_git"}
            )

        elif direction == "bidirectional":

            sync_status = gestionnaire.etats_synchro.get(element_id, "conflict")
            deployable_now = (
                not protege
                and sync_status == "git_ahead"
                and etat in {"different", "missing_ha"}
            )
            pushable_now = (
                sync_status == "ha_ahead"
                and etat in {"different", "missing_git"}
            )

        normalizable_now = (
            kind != "directory"
            and direction in {"ha_to_git", "bidirectional"}
            and etat == "equivalent"
        )

        manually_resolvable_now = (
            direction == "bidirectional"
            and etat in {"different", "missing_ha", "missing_git"}
            and sync_status != "conflict"
        )

        previewable_now = (
            kind != "directory"
            and direction in {"git_to_ha", "ha_to_git"}
            and etat in {"different", "missing_ha", "missing_git"}
        )

        restaurable_now = kind != "directory" and direction in {"git_to_ha", "bidirectional"}

        fichiers.append(
            {
                "id": element_id,
                "kind": kind,
                "direction": direction,
                "state": etat,
                "protected": protege,
                "deployable_now": deployable_now,
                "sync_status": sync_status,
                "pushable_now": pushable_now,
                "normalizable_now": normalizable_now,
                "manually_resolvable_now": manually_resolvable_now,
                "previewable_now": previewable_now,
                "restaurable_now": restaurable_now,
                "normalize_line_endings": bool(element.get("normalize_line_endings")),
                "ha_path": element["ha_path"],
                "git_path": element["git_path"],
            }
        )

    return {
        "ok": True,
        "application_version": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": informations_git(),
        "summary": {
            "managed": len(gestionnaire.elements),
            "identical": sum(1 for f in fichiers if f["state"] == "identical"),
            "equivalent": sum(1 for f in fichiers if f["state"] == "equivalent"),
            "different": sum(1 for f in fichiers if f["state"] == "different"),
            "deployable_now": sum(1 for f in fichiers if f["deployable_now"]),
        },
        "elements": fichiers,
    }


###############################################################################
# HTTP SERVER
###############################################################################

class InterfaceHandler(BaseHTTPRequestHandler):

    server_version = "HomelabGitManagement/2.4.0"

    def envoyer_entetes(self, statut: int, type_contenu: str) -> None:

        self.send_response(statut)
        self.send_header("Content-Type", type_contenu)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def repondre_json(self, statut: int, donnees: dict) -> None:

        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.envoyer_entetes(statut, "application/json; charset=utf-8")
        self.wfile.write(corps)

    def repondre_telechargement(self, contenu: bytes, nom_fichier: str, type_contenu: str) -> None:

        self.send_response(200)
        self.send_header("Content-Type", type_contenu)
        self.send_header("Content-Disposition", f'attachment; filename="{nom_fichier}"')
        self.send_header("Content-Length", str(len(contenu)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(contenu)

    def lire_corps_borne(self, max_bytes: int) -> bytes | None:
        """Reads the request body, bounded to `max_bytes`. Responds with
        an error and returns None on an invalid or oversized
        Content-Length — every caller must check for None before using
        the result."""

        try:
            longueur = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.repondre_json(400, {"ok": False, "error": "invalid Content-Length"})
            return None

        if longueur < 0:
            self.repondre_json(400, {"ok": False, "error": "invalid Content-Length"})
            return None

        if longueur > max_bytes:
            self.repondre_json(413, {"ok": False, "error": "payload too large"})
            return None

        return self.rfile.read(longueur) if longueur > 0 else b"{}"

    def do_GET(self) -> None:

        chemin = self.path.split("?", 1)[0]

        if chemin.endswith("/api/setup"):
            self.repondre_json(200, construire_setup())
            return

        if chemin.endswith("/api/status"):
            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/browse"):
            gerer_browse(self)
            return

        if chemin.endswith("/api/diff"):
            gerer_diff(self)
            return

        if chemin.endswith("/api/directory-plan"):
            gerer_plan_repertoire(self)
            return

        if chemin.endswith("/api/backups"):
            gerer_liste_sauvegardes(self)
            return

        if chemin.endswith("/api/mappings/export.xlsx"):
            gerer_export_excel(self)
            return

        if chemin.endswith("/health"):
            self.envoyer_entetes(200, "application/json; charset=utf-8")
            self.wfile.write(b'{"status":"ok"}')
            return

        corps = HTML_PAGE.encode("utf-8")
        self.envoyer_entetes(200, "text/html; charset=utf-8")
        self.wfile.write(corps)

    def do_POST(self) -> None:

        chemin = self.path.split("?", 1)[0]

        if chemin.endswith("/api/refresh"):

            succes, message_erreur = declencher_rafraichissement_git()

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/deploy"):

            corps_requete = self.lire_corps_borne(REQUEST_PAYLOAD_MAX_BYTES)

            if corps_requete is None:
                return

            try:
                charge = json.loads(corps_requete)
            except json.JSONDecodeError:
                charge = {}

            cible = charge.get("target") if isinstance(charge, dict) else None
            forcer = charge.get("force", False) if isinstance(charge, dict) else False
            sens = charge.get("sens") if isinstance(charge, dict) else None

            succes, message_erreur = declencher_deploiement(cible, forcer, sens)

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/acknowledge-git"):

            corps_requete = self.lire_corps_borne(REQUEST_PAYLOAD_MAX_BYTES)

            if corps_requete is None:
                return

            try:
                charge = json.loads(corps_requete)
            except json.JSONDecodeError:
                charge = {}

            cible = charge.get("target") if isinstance(charge, dict) else None

            succes, message_erreur = declencher_acquittement_git(cible)

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/normalize-git"):

            corps_requete = self.lire_corps_borne(REQUEST_PAYLOAD_MAX_BYTES)

            if corps_requete is None:
                return

            try:
                charge = json.loads(corps_requete)
            except json.JSONDecodeError:
                charge = {}

            cible = charge.get("target") if isinstance(charge, dict) else None

            succes, message_erreur = declencher_normalisation_git(cible)

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/restore-backup"):

            corps_requete = self.lire_corps_borne(REQUEST_PAYLOAD_MAX_BYTES)

            if corps_requete is None:
                return

            try:
                charge = json.loads(corps_requete)
            except json.JSONDecodeError:
                charge = {}

            cible = charge.get("target") if isinstance(charge, dict) else None
            nom_sauvegarde = charge.get("backup") if isinstance(charge, dict) else None

            succes, message_erreur = declencher_restauration_sauvegarde(cible, nom_sauvegarde)

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
            return

        if chemin.endswith("/api/mappings/import-excel-preview"):
            gerer_import_excel_preview(self)
            return

        if chemin.endswith("/api/mappings"):
            gerer_sauvegarde_mappings(self)
            return

        self.repondre_json(405, {"error": "unknown endpoint"})

    def log_message(self, format_string, *arguments) -> None:
        print(f"[homelab-git-management] [interface] {format_string % arguments}", flush=True)


###############################################################################
# STARTUP
###############################################################################

if __name__ == "__main__":

    print(f"[homelab-git-management] Ingress interface: listening on {HOST}:{PORT}", flush=True)

    serveur = HTTPServer((HOST, PORT), InterfaceHandler)
    serveur.serve_forever()

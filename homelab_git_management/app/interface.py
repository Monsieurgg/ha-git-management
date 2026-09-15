from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


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
.header-actions { display: flex; gap: 8px; }
button { border: 1px solid var(--border); background: var(--surface); color: var(--text);
  border-radius: 10px; padding: 10px 15px; cursor: pointer; font: inherit; }
button:hover { border-color: var(--accent); }
button:disabled { opacity: 0.5; cursor: default; }
.action-deploy { margin-left: 8px; padding: 4px 10px; font-size: 12px; border-color: var(--candidate); color: var(--candidate); }
.summary { display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 15px; box-shadow: var(--shadow); }
.card-label { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.card-value { margin-top: 6px; font-size: 26px; font-weight: 700; }
.card-meta { margin-top: 5px; color: var(--muted); font-size: 12px; }
.panel { overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); margin-bottom: 18px; }
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 15px 17px; border-bottom: 1px solid var(--border); }
.panel-header h2 { margin: 0; font-size: 17px; }
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
.action-edit, .action-delete { margin-right: 6px; padding: 4px 10px; font-size: 12px; }
.action-delete { border-color: var(--danger); color: var(--danger); }
.modal-overlay { display: flex; align-items: center; justify-content: center; position: fixed; inset: 0;
  background: rgba(15,23,42,0.45); z-index: 50; padding: 16px; }
.modal { background: var(--surface); border-radius: 14px; box-shadow: var(--shadow); width: 100%;
  max-width: 480px; max-height: 88vh; display: flex; flex-direction: column; overflow: hidden; }
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
        <h2 data-i18n="table_title">Managed elements status</h2>
        <div id="status-line" class="status-line" data-i18n="loading">Loading…</div>
      </div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th data-i18n="th_element">Element</th>
              <th data-i18n="th_kind">Type</th>
              <th data-i18n="th_direction">Direction</th>
              <th data-i18n="th_comparison">Comparison</th>
              <th data-i18n="th_protection">Protection</th>
              <th data-i18n="th_action">Action</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2 data-i18n="mappings_title">Configured mappings</h2>
        <button id="add-mapping" type="button" data-i18n="mappings_add">+ Add a mapping</button>
      </div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th data-i18n="th_element">Element</th>
              <th data-i18n="th_ha_path">HA path</th>
              <th data-i18n="th_git_path">Git path</th>
              <th data-i18n="th_direction">Direction</th>
              <th data-i18n="th_manage">Manage</th>
            </tr>
          </thead>
          <tbody id="mapping-rows"></tbody>
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
        <option value="ha_to_git" data-i18n="dir_ha_to_git" disabled>Home Assistant -&gt; Git (coming soon)</option>
        <option value="bidirectional" data-i18n="dir_bidirectional" disabled>Bidirectional (coming soon)</option>
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

<script>
const STRINGS = {
  en: {
    title: "Homelab Git Management",
    subtitle: "Git <-> Home Assistant control",
    refresh: "Refresh", refreshing: "Refreshing...",
    readonly_banner: "The Refresh button updates the Git clone (read-only from GitHub). The Deploy button, shown only for allowed elements, writes to Home Assistant after explicit confirmation.",
    card_app: "Application", card_app_meta: "runtime version",
    card_managed: "Managed elements", card_managed_meta: "mappings",
    card_identical: "Identical", card_identical_meta: "strict equality",
    card_different: "Different", card_different_meta: "actual content",
    card_candidates: "To deploy", card_candidates_meta: "allowed candidates",
    table_title: "Managed elements status", loading: "Loading…",
    th_element: "Element", th_kind: "Type", th_direction: "Direction",
    th_comparison: "Comparison", th_protection: "Protection", th_action: "Action",
    state_identical: "IDENTICAL", state_equivalent: "EQUIVALENT", state_different: "DIFFERENT",
    state_missing_ha: "MISSING ON HA", state_missing_git: "MISSING IN GIT",
    state_missing_both: "MISSING FROM BOTH", state_error: "ERROR", state_unknown: "UNKNOWN",
    dir_forbidden: "FORBIDDEN", dir_candidate: "CANDIDATE", dir_after_confirmation: "CONFIGURED",
    protected: "PROTECTED", standard: "STANDARD",
    action_required: "DEPLOYMENT REQUIRED", action_deploy: "Deploy",
    action_none: "NONE", action_blocked: "BLOCKED",
    error_load: "Could not load status: ", error_refresh: "Git refresh failed: ",
    error_deploy: "Deployment failed: ",
    deploying: "Deploying...",
    confirm_deploy: 'Deploy "{target}" from GitHub to Home Assistant?\n\nA backup of the current file will be created before writing. If verification fails, an automatic rollback restores the previous content.',
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
    mappings_title: "Configured mappings",
    mappings_add: "+ Add a mapping",
    th_ha_path: "HA path", th_git_path: "Git path", th_manage: "Manage",
    mapping_edit: "Edit", mapping_delete: "Delete",
    mapping_modal_add: "Add a mapping", mapping_modal_edit: "Edit mapping",
    field_id: "Identifier", field_kind: "Type", field_direction: "Direction",
    field_ha_path: "Home Assistant path", field_git_path: "Git path",
    kind_file: "File", kind_directory: "Directory (comparison only)",
    dir_git_to_ha: "Git -> Home Assistant",
    dir_ha_to_git: "Home Assistant -> Git (coming soon)",
    dir_bidirectional: "Bidirectional (coming soon)",
    browse: "Browse…", browse_title: "Browse",
    browse_select_here: "Select this folder",
    cancel: "Cancel", save: "Save",
    mapping_error_incomplete: "Please fill in the identifier, the HA path and the Git path.",
    mapping_confirm_delete: 'Delete mapping "{id}"?\n\nThis only removes it from the configuration — no file is touched.',
    mapping_error_delete: "Could not delete: ",
  },
  fr: {
    title: "Homelab Git Management",
    subtitle: "Contrôle Git ↔ Home Assistant",
    refresh: "Actualiser", refreshing: "Actualisation...",
    readonly_banner: "Le bouton Actualiser met à jour le clone Git (lecture GitHub uniquement). Le bouton Déployer, visible uniquement sur les éléments autorisés, écrit sur Home Assistant après confirmation explicite.",
    card_app: "Application", card_app_meta: "version runtime",
    card_managed: "Éléments gérés", card_managed_meta: "mappings",
    card_identical: "Identiques", card_identical_meta: "égalité stricte",
    card_different: "Différents", card_different_meta: "contenu réel",
    card_candidates: "À déployer", card_candidates_meta: "candidats autorisés",
    table_title: "État des éléments gérés", loading: "Chargement…",
    th_element: "Élément", th_kind: "Type", th_direction: "Direction",
    th_comparison: "Comparaison", th_protection: "Protection", th_action: "Action",
    state_identical: "IDENTIQUE", state_equivalent: "ÉQUIVALENT", state_different: "DIFFÉRENT",
    state_missing_ha: "ABSENT HA", state_missing_git: "ABSENT GIT",
    state_missing_both: "ABSENT DES DEUX", state_error: "ERREUR", state_unknown: "INCONNU",
    dir_forbidden: "INTERDIT", dir_candidate: "CANDIDAT", dir_after_confirmation: "CONFIGURÉ",
    protected: "PROTÉGÉ", standard: "STANDARD",
    action_required: "DÉPLOIEMENT REQUIS", action_deploy: "Déployer",
    action_none: "AUCUNE", action_blocked: "BLOQUÉE",
    error_load: "Impossible de charger l'état : ", error_refresh: "Échec de l'actualisation Git : ",
    error_deploy: "Échec du déploiement : ",
    deploying: "Déploiement...",
    confirm_deploy: 'Déployer « {target} » de GitHub vers Home Assistant ?\n\nUne sauvegarde du fichier actuel sera créée avant l\'écriture. En cas d\'échec de la vérification, un rollback automatique restaure l\'ancien contenu.',
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
    mappings_title: "Mappings configurés",
    mappings_add: "+ Ajouter un mapping",
    th_ha_path: "Chemin HA", th_git_path: "Chemin Git", th_manage: "Gérer",
    mapping_edit: "Modifier", mapping_delete: "Supprimer",
    mapping_modal_add: "Ajouter un mapping", mapping_modal_edit: "Modifier le mapping",
    field_id: "Identifiant", field_kind: "Type", field_direction: "Direction",
    field_ha_path: "Chemin Home Assistant", field_git_path: "Chemin Git",
    kind_file: "Fichier", kind_directory: "Dossier (comparaison uniquement)",
    dir_git_to_ha: "Git -> Home Assistant",
    dir_ha_to_git: "Home Assistant -> Git (bientôt disponible)",
    dir_bidirectional: "Bidirectionnel (bientôt disponible)",
    browse: "Parcourir…", browse_title: "Parcourir",
    browse_select_here: "Choisir ce dossier",
    cancel: "Annuler", save: "Enregistrer",
    mapping_error_incomplete: "Merci de renseigner l'identifiant, le chemin HA et le chemin Git.",
    mapping_confirm_delete: 'Supprimer le mapping « {id} » ?\n\nCela retire uniquement la configuration — aucun fichier n\'est touché.',
    mapping_error_delete: "Impossible de supprimer : ",
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

function badgeDirection(fichier) {
  if (fichier.direction !== "git_to_ha") return badge(t("dir_forbidden"), "blocked");
  if (fichier.deployable_now) return badge(t("dir_candidate"), "candidate");
  return badge(t("dir_after_confirmation"), "neutral");
}

function cheminBaseIngress() {
  return window.location.pathname.endsWith("/") ? window.location.pathname : `${window.location.pathname}/`;
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
    const code = document.createElement("code");
    code.textContent = fichier.id;
    idCell.appendChild(code);
    tr.appendChild(idCell);

    for (const value of [fichier.kind]) {
      const td = document.createElement("td");
      td.textContent = value ?? "—";
      tr.appendChild(td);
    }

    const dirCell = document.createElement("td");
    dirCell.textContent = fichier.direction;
    tr.appendChild(dirCell);

    const cmpCell = document.createElement("td");
    cmpCell.appendChild(badgeEtat(fichier.state));
    tr.appendChild(cmpCell);

    const protCell = document.createElement("td");
    protCell.appendChild(fichier.protected ? badge(t("protected"), "blocked") : badge(t("standard"), "neutral"));
    tr.appendChild(protCell);

    const actionCell = document.createElement("td");
    if (fichier.deployable_now) {
      actionCell.appendChild(badge(t("action_required"), "candidate"));
      const btn = document.createElement("button");
      btn.className = "action-deploy";
      btn.textContent = t("action_deploy");
      btn.addEventListener("click", () => deployerElement(fichier.id, btn));
      actionCell.appendChild(btn);
    } else if (["identical", "equivalent"].includes(fichier.state)) {
      actionCell.appendChild(badge(t("action_none"), "identical"));
    } else {
      actionCell.appendChild(badge(t("action_blocked"), "neutral"));
    }
    tr.appendChild(actionCell);

    tbody.appendChild(tr);
  }

  mappingsActuels = donnees.elements;
  afficherMappings();
}

function afficherMappings() {
  const tbody = el("mapping-rows");
  tbody.replaceChildren();

  for (const mapping of mappingsActuels) {
    const tr = document.createElement("tr");

    const idCell = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = mapping.id;
    idCell.appendChild(code);
    tr.appendChild(idCell);

    const haCell = document.createElement("td");
    haCell.textContent = mapping.ha_path;
    tr.appendChild(haCell);

    const gitCell = document.createElement("td");
    gitCell.textContent = mapping.git_path;
    tr.appendChild(gitCell);

    const dirCell = document.createElement("td");
    dirCell.textContent = mapping.direction;
    tr.appendChild(dirCell);

    const actionCell = document.createElement("td");

    const editBtn = document.createElement("button");
    editBtn.className = "action-edit";
    editBtn.textContent = t("mapping_edit");
    editBtn.addEventListener("click", () => ouvrirModalMapping(mapping));
    actionCell.appendChild(editBtn);

    const delBtn = document.createElement("button");
    delBtn.className = "action-delete";
    delBtn.textContent = t("mapping_delete");
    delBtn.addEventListener("click", () => supprimerMapping(mapping.id));
    actionCell.appendChild(delBtn);

    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  }
}

function copierMappingsPourEnvoi() {
  return mappingsActuels.map((mapping) => ({
    id: mapping.id, kind: mapping.kind, direction: mapping.direction,
    ha_path: mapping.ha_path, git_path: mapping.git_path,
  }));
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

  el("mapping-modal").hidden = false;
}

function fermerModalMapping() {
  el("mapping-modal").hidden = true;
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

  const nouveauMapping = { id, kind, direction, ha_path: haPath, git_path: gitPath };
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
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = exception.message;
    erreur.style.display = "block";
  } finally {
    boutonSave.disabled = false;
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
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("mapping_error_delete") + exception.message;
    erreur.style.display = "block";
  }
}

function ouvrirBrowse(racine, cheminActuel) {
  browseRacine = racine;
  browseCheminCourant = cheminActuel && cheminActuel.trim()
    ? cheminActuel.trim()
    : (racine === "ha" ? "/config" : "");

  el("browse-modal").hidden = false;
  chargerBrowse();
}

function fermerBrowse() {
  el("browse-modal").hidden = true;
}

async function chargerBrowse() {
  const erreur = el("browse-error");
  erreur.style.display = "none";
  el("browse-list").replaceChildren();

  try {
    const parametres = new URLSearchParams({ root: browseRacine, path: browseCheminCourant });
    const reponse = await fetch(`${cheminBaseIngress()}api/browse?${parametres}`, { cache: "no-store" });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);

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

      return;
    }

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

async function deployerElement(cible, bouton) {
  if (!window.confirm(t("confirm_deploy").replace("{target}", cible))) return;

  const erreur = el("error");
  erreur.style.display = "none";
  bouton.disabled = true;
  const libelle = bouton.textContent;
  bouton.textContent = t("deploying");

  try {
    const reponse = await fetch(`${cheminBaseIngress()}api/deploy`, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: cible }),
    });
    const donnees = await reponse.json();
    if (!reponse.ok || !donnees.ok) throw new Error(donnees.error || `HTTP ${reponse.status}`);
    afficherEtat(donnees);
  } catch (exception) {
    erreur.textContent = t("error_deploy") + exception.message;
    erreur.style.display = "block";
    bouton.disabled = false;
    bouton.textContent = libelle;
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

el("lang-toggle").addEventListener("click", switchLang);
el("refresh").addEventListener("click", actualiserGit);
el("copy-key").addEventListener("click", copierCle);

el("add-mapping").addEventListener("click", () => ouvrirModalMapping(null));
el("mapping-modal-close").addEventListener("click", fermerModalMapping);
el("mapping-cancel").addEventListener("click", fermerModalMapping);
el("mapping-save").addEventListener("click", sauvegarderMapping);

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

    config_url = None

    if not repo:

        slug = obtenir_slug_propre()

        if slug:
            config_url = f"/config/app/{slug}/config"

    return {
        "ok": True,
        "configured": bool(repo),
        "github_repository": repo,
        "github_branch": options.get("github_branch") or "main",
        "public_key": public_key,
        "config_url": config_url,
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

MAPPINGS_PAYLOAD_MAX_BYTES = 262144  # 256 KiB: generous, but bounded


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
                "(only git_to_ha is implemented) — this would stop the "
                "engine from starting at all if saved"
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

        if kind == "file" and module.est_chemin_protege(chemin_ha):
            raise ValueError(
                f"{element_id}: this is a core Home Assistant config file "
                "and can never be managed by a mapping"
            )

        resultat.append({
            "id": element_id,
            "kind": kind,
            "ha_path": ha_path.strip(),
            "git_path": git_path.strip(),
            "direction": direction,
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


def gerer_sauvegarde_mappings(handler: "InterfaceHandler") -> None:

    longueur = int(handler.headers.get("Content-Length", 0) or 0)

    if longueur > MAPPINGS_PAYLOAD_MAX_BYTES:
        handler.repondre_json(413, {"ok": False, "error": "payload too large"})
        return

    corps_requete = handler.rfile.read(longueur) if longueur > 0 else b"{}"

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

    succes, erreur_ecriture = ecrire_options_supervisor({"mappings": mappings_valides})

    if not succes:
        handler.repondre_json(502, {"ok": False, "error": erreur_ecriture})
        return

    print(
        f"[homelab-git-management] [interface] mappings updated via UI "
        f"({len(mappings_valides)} entries)",
        flush=True,
    )

    donnees = construire_etat()
    handler.repondre_json(200 if donnees.get("ok") else 503, donnees)


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

def declencher_deploiement(cible: object) -> tuple[bool, str | None]:

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

            module.deployer_element(cible, True)

    except SystemExit as exc:

        message = sortie_capturee.getvalue().strip()

        print(sortie_capturee.getvalue(), end="", flush=True)

        return False, message or f"deployment refused (see add-on logs): code {exc.code}"

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
        protege = gestionnaire.est_chemin_protege(chemin_ha)
        direction = element["direction"]
        kind = element["kind"]

        deployable_now = (
            not protege
            and kind != "directory"
            and direction == "git_to_ha"
            and etat in {"different", "missing_ha"}
        )

        fichiers.append(
            {
                "id": element_id,
                "kind": kind,
                "direction": direction,
                "state": etat,
                "protected": protege,
                "deployable_now": deployable_now,
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

    server_version = "HomelabGitManagement/0.2.0"

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

            longueur = int(self.headers.get("Content-Length", 0) or 0)
            corps_requete = self.rfile.read(longueur) if longueur > 0 else b"{}"

            try:
                charge = json.loads(corps_requete)
            except json.JSONDecodeError:
                charge = {}

            cible = charge.get("target") if isinstance(charge, dict) else None

            succes, message_erreur = declencher_deploiement(cible)

            if not succes:
                self.repondre_json(502, {"ok": False, "error": message_erreur})
                return

            donnees = construire_etat()
            self.repondre_json(200 if donnees.get("ok") else 503, donnees)
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

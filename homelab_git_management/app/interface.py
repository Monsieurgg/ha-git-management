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
from pathlib import Path
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys


###############################################################################
# CONSTANTS
###############################################################################

HOST = "0.0.0.0"
PORT = 8099

APP_VERSION = os.environ.get("APP_VERSION", "unknown")

GIT_ROOT = Path("/data/repository")
OPTIONS_PATH = Path("/data/options.json")
SSH_PUBLIC_KEY = Path("/data/ssh/github_deploy_key.pub")


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
      <p id="setup-body" data-i18n="setup_body"></p>
      <div class="field-row"><span class="label" data-i18n="setup_key_label">Public key</span></div>
      <code id="setup-key" class="key-box">—</code>
      <div class="field-row"><span class="label" data-i18n="setup_repo_label">Configured repository</span><span id="setup-repo">—</span></div>
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
    setup_body: "No GitHub repository is configured yet. Copy the public key below and add it as a read-only Deploy Key on your repository (GitHub -> Settings -> Deploy keys -> Add deploy key), then set github_repository (owner/repository) and github_branch in this add-on's Configuration tab and restart it.",
    setup_key_label: "Public key:",
    setup_repo_label: "Configured repository:",
    setup_branch_label: "Configured branch:",
    setup_none: "not set",
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
    setup_body: "Aucun dépôt GitHub n'est configuré. Copiez la clé publique ci-dessous et ajoutez-la comme Deploy Key en lecture seule sur votre dépôt (GitHub -> Settings -> Deploy keys -> Add deploy key), puis renseignez github_repository (owner/repository) et github_branch dans l'onglet Configuration de cette Application et redémarrez-la.",
    setup_key_label: "Clé publique :",
    setup_repo_label: "Dépôt configuré :",
    setup_branch_label: "Branche configurée :",
    setup_none: "non défini",
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

el("lang-toggle").addEventListener("click", switchLang);
el("refresh").addEventListener("click", actualiserGit);

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


def construire_setup() -> dict:

    options = lire_options()
    repo = options.get("github_repository") or ""

    public_key = None

    if SSH_PUBLIC_KEY.is_file():
        try:
            public_key = SSH_PUBLIC_KEY.read_text(encoding="utf-8").strip()
        except Exception:
            public_key = None

    return {
        "ok": True,
        "configured": bool(repo),
        "github_repository": repo,
        "github_branch": options.get("github_branch") or "main",
        "public_key": public_key,
    }


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
        return False, f"Git update refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

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

        return False, message or f"deployment refused (see add-on logs): code {exc.code}"

    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

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

    server_version = "HomelabGitManagement/0.1.0"

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

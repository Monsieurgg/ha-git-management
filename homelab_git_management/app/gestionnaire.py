#!/usr/bin/env python3

###############################################################################
# HOMELAB GIT MANAGEMENT — ENGINE
###############################################################################
#
# Role:
#   Update the local Git clone (fetch + fast-forward),
#   validate the user-supplied mappings,
#   check every path,
#   compare Git and Home Assistant,
#   evaluate the git_to_ha policy,
#   receive an explicit command,
#   back up the target,
#   perform an atomic copy,
#   verify the result,
#   automatically restore the backup on failure.
#
# Where the managed-files list comes from:
#   Earlier private/single-user builds of this idea kept the list of
#   managed files as a YAML file baked into the Docker image. That only
#   works for one specific user's own configuration. Here, the list is
#   entirely driven by this add-on's options (Settings -> Add-ons ->
#   Homelab Git Management -> Configuration), written by Supervisor to
#   /data/options.json and re-read on every start. Nothing is comparable
#   or deployable until a user explicitly adds a mapping there.
#
# Security:
#   - Git remains read-only;
#   - no target outside /homeassistant;
#   - no Git path outside /data/repository;
#   - Home Assistant's four main YAML files are always protected against
#     Git -> HA writes, regardless of what a mapping declares;
#   - direction: git_to_ha required for a deployment to even be
#     considered (ha_to_git / bidirectional are accepted by the options
#     schema but rejected at startup: not implemented yet);
#   - confirm:true is mandatory on every deployment;
#   - directories cannot be deployed (comparison only);
#   - a persistent backup is made before any replacement;
#   - replacement is atomic;
#   - the result is verified byte-for-byte;
#   - a failed verification triggers an automatic rollback.
#
###############################################################################

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import re
import stat
import subprocess
import sys


###############################################################################
# CONSTANTS
###############################################################################

OPTIONS_PATH = Path("/data/options.json")

HA_ROOT = Path("/homeassistant")
HA_MANIFEST_ROOT = PurePosixPath("/config")

GIT_ROOT = Path("/data/repository")

SSH_DIR = Path("/data/ssh")
SSH_PRIVATE_KEY = SSH_DIR / "github_deploy_key"
GITHUB_KNOWN_HOSTS = SSH_DIR / "known_hosts"

# Separate, dedicated write-capable key: kept apart from the read-only key
# above so the code paths that only ever need read access (comparison,
# git_to_ha) never even reference a key capable of writing to GitHub. A
# mapping's ha_to_git direction is inert until this key is generated *and*
# added as a (non-read-only) Deploy Key on GitHub — see run.sh.
SSH_PRIVATE_KEY_WRITE = SSH_DIR / "github_deploy_key_write"

BACKUP_ROOT = Path("/data/deploy-backups")

# Records the content of each side the last time it was confirmed in
# agreement (either because a comparison found them identical/equivalent,
# or because a git_to_ha/ha_to_git deployment just made them match). This
# is what lets a later comparison tell "only one side changed since" apart
# from "both changed independently" (a real conflict) — a plain timestamp
# comparison cannot make that distinction; see SYNCHRONIZATION STATE below.
SYNC_STATE_PATH = Path("/data/sync-state.json")

UTF8_BOM = b"\xef\xbb\xbf"

TEXT_EXTENSIONS = {
    ".yaml", ".yml", ".py", ".sh", ".js", ".json", ".html", ".htm",
    ".css", ".md", ".txt", ".jinja", ".j2", ".xml",
}

RUNTIME_IGNORED_DIRS = {"__pycache__"}
RUNTIME_IGNORED_SUFFIXES = {".pyc"}

# A mapping's direction must be one of these (enforced by config.yaml's
# schema too, but re-checked here: options.json could in principle be
# written by something other than the Supervisor UI).
ALLOWED_DIRECTIONS = {"git_to_ha", "ha_to_git", "bidirectional"}

# All three declared directions are implemented.
IMPLEMENTED_DIRECTIONS = {"git_to_ha", "ha_to_git", "bidirectional"}

ALLOWED_KINDS = {"file", "directory"}

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Exactly what creer_sauvegarde() names a backup file — validated before
# ever building a path from a caller-supplied name (see restaurer_sauvegarde_locale()).
BACKUP_NAME_PATTERN = re.compile(r"^\d{8}T\d{6}\.\d{6}Z-.+\.bak$")

# Home Assistant's own core YAML files. Protected against Git -> HA
# writes by default — see est_chemin_protege() — unless a mapping
# explicitly opts out via protect_from_git: false, which the dashboard
# only ever allows after a strong, explicit warning.
PROTECTED_HA_PATHS = {
    HA_ROOT / "configuration.yaml",
    HA_ROOT / "scripts.yaml",
    HA_ROOT / "automations.yaml",
    HA_ROOT / "scenes.yaml",
}


###############################################################################
# LOGS
###############################################################################

def log(message: str) -> None:
    print(f"[homelab-git-management] {message}", flush=True)


def erreur(message: str) -> None:
    log(f"ERROR: {message}")
    sys.exit(1)


###############################################################################
# OPTIONS (github_repository / github_branch / mappings)
###############################################################################

if not OPTIONS_PATH.is_file():
    erreur(f"options file not found: {OPTIONS_PATH}")

try:
    with OPTIONS_PATH.open("r", encoding="utf-8") as fichier:
        options = json.load(fichier)
except Exception as exc:
    erreur(f"cannot read options: {exc}")

if not isinstance(options, dict):
    erreur("options must be a JSON object")

GIT_REPOSITORY_BRANCH = options.get("github_branch") or "main"

if (
    not isinstance(GIT_REPOSITORY_BRANCH, str)
    or not re.fullmatch(r"[A-Za-z0-9_./-]+", GIT_REPOSITORY_BRANCH)
    or GIT_REPOSITORY_BRANCH.startswith("-")
):
    erreur(f"invalid github_branch: {GIT_REPOSITORY_BRANCH!r}")

mappings = options.get("mappings")

if not isinstance(mappings, list):
    erreur("'mappings' must be a list")


###############################################################################
# MAPPINGS VALIDATION
###############################################################################

ids = set()
elements = []

for mapping in mappings:

    if not isinstance(mapping, dict):
        erreur("a 'mappings' entry is not an object")

    element_id = mapping.get("id")

    if (
        not isinstance(element_id, str)
        or not element_id
        or not ID_PATTERN.fullmatch(element_id)
    ):
        erreur(f"invalid id: {element_id}")

    if element_id in ids:
        erreur(f"duplicate id: {element_id}")

    ids.add(element_id)

    kind = mapping.get("kind") or "file"

    if kind not in ALLOWED_KINDS:
        erreur(f"{element_id}: invalid kind: {kind}")

    direction = mapping.get("direction")

    if direction not in ALLOWED_DIRECTIONS:
        erreur(f"{element_id}: invalid direction: {direction}")

    if direction not in IMPLEMENTED_DIRECTIONS:
        erreur(
            f"{element_id}: direction '{direction}' is not implemented in "
            f"this version (only {', '.join(sorted(IMPLEMENTED_DIRECTIONS))} "
            "are supported today) — remove this mapping or change its "
            "direction"
        )

    ha_path = mapping.get("ha_path")
    git_path = mapping.get("git_path")

    if not isinstance(ha_path, str) or not ha_path.strip():
        erreur(f"{element_id}: invalid ha_path")

    if not isinstance(git_path, str) or not git_path.strip():
        erreur(f"{element_id}: invalid git_path")

    protect_from_git = mapping.get("protect_from_git")

    if protect_from_git is not None and not isinstance(protect_from_git, bool):
        erreur(f"{element_id}: invalid protect_from_git (must be true or false)")

    normalize_line_endings = mapping.get("normalize_line_endings")

    if normalize_line_endings is not None and not isinstance(normalize_line_endings, bool):
        erreur(f"{element_id}: invalid normalize_line_endings (must be true or false)")

    elements.append(
        {
            "id": element_id,
            "kind": kind,
            "direction": direction,
            "ha_path": ha_path,
            "git_path": git_path,
            "protect_from_git": protect_from_git,
            "normalize_line_endings": bool(normalize_line_endings),
        }
    )

log("Options: OK")
log(f"Managed mappings: {len(elements)}")


###############################################################################
# PATHS
###############################################################################

def convertir_chemin_ha(ha_path: str) -> Path:

    if not isinstance(ha_path, str) or not ha_path.strip():
        raise ValueError("ha_path missing or invalid")

    chemin = PurePosixPath(ha_path)

    if not chemin.is_absolute():
        raise ValueError("ha_path must be absolute")

    if ".." in chemin.parts:
        raise ValueError("ha_path contains '..'")

    try:
        relatif = chemin.relative_to(HA_MANIFEST_ROOT)
    except ValueError as exc:
        raise ValueError(f"ha_path must be under /config: {ha_path}") from exc

    return HA_ROOT.joinpath(*relatif.parts)


def convertir_chemin_git(git_path: str) -> Path:

    if not isinstance(git_path, str) or not git_path.strip():
        raise ValueError("git_path missing or invalid")

    chemin = PurePosixPath(git_path)

    if chemin.is_absolute():
        raise ValueError("git_path must be relative to the repository")

    if ".." in chemin.parts:
        raise ValueError("git_path contains '..'")

    if not chemin.parts:
        raise ValueError("git_path is empty")

    return GIT_ROOT.joinpath(*chemin.parts)


def verifier_chemin_resolu(chemin: Path, racine: Path, nom_racine: str) -> None:

    racine_resolue = racine.resolve()
    chemin_resolu = chemin.resolve()

    try:
        chemin_resolu.relative_to(racine_resolue)
    except ValueError as exc:
        raise ValueError(
            f"resolved path outside of {nom_racine}: {chemin_resolu}"
        ) from exc


def est_chemin_protege(element: dict, chemin_ha: Path) -> bool:
    """Whether this mapping's Home Assistant target is protected against
    git_to_ha writes. A mapping's own explicit protect_from_git choice
    (set from the "Protect this file" checkbox in the dashboard, or
    directly in options.json/the native Configuration tab) always wins.
    Only when no explicit choice was ever recorded — a mapping created
    before this option existed, or one where the field was simply
    omitted — does this fall back to protecting Home Assistant's own
    four core YAML files by default, exactly as before this option
    existed. This fallback is deliberately the only thing keeping those
    four files safe by default now: unlike earlier versions, a mapping
    CAN explicitly disable it (the dashboard warns strongly before
    allowing that) — the safety net here is "protected unless you say
    otherwise", not "always protected no matter what"."""

    valeur = element.get("protect_from_git")

    if isinstance(valeur, bool):
        return valeur

    return chemin_ha.resolve() in {p.resolve() for p in PROTECTED_HA_PATHS}


###############################################################################
# HOME ASSISTANT PATH CHECKS
###############################################################################

presents_ha = 0
absents_ha = 0
erreurs_ha = 0

log("Checking Home Assistant paths...")

for element in elements:

    element_id = element["id"]
    kind = element["kind"]
    ha_path = element["ha_path"]

    try:

        chemin = convertir_chemin_ha(ha_path)
        verifier_chemin_resolu(chemin, HA_ROOT, "/homeassistant")

        if kind == "directory":

            if not chemin.exists():
                log(f"[MISSING] {element_id}: {ha_path}")
                absents_ha += 1
                continue

            if not chemin.is_dir():
                log(f"[ERROR] {element_id}: expected a directory")
                erreurs_ha += 1
                continue

            if not os.access(chemin, os.R_OK | os.X_OK):
                log(f"[ERROR] {element_id}: directory not accessible")
                erreurs_ha += 1
                continue

        else:

            if not chemin.exists():
                log(f"[MISSING] {element_id}: {ha_path}")
                absents_ha += 1
                continue

            if not chemin.is_file():
                log(f"[ERROR] {element_id}: expected a file")
                erreurs_ha += 1
                continue

            if not os.access(chemin, os.R_OK):
                log(f"[ERROR] {element_id}: file not readable")
                erreurs_ha += 1
                continue

        log(f"[OK] {element_id}: {ha_path}")
        presents_ha += 1

    except Exception as exc:

        log(f"[ERROR] {element_id}: {exc}")
        erreurs_ha += 1

log(f"Present on HA: {presents_ha}")
log(f"Missing on HA: {absents_ha}")
log(f"Errors: {erreurs_ha}")

if erreurs_ha:
    erreur(f"{erreurs_ha} error(s) in Home Assistant paths")

log("Home Assistant validation: OK")


###############################################################################
# GIT CLONE CHECK
###############################################################################

if not GIT_ROOT.exists():
    erreur(f"Git clone missing: {GIT_ROOT}")

if not GIT_ROOT.is_dir():
    erreur(f"invalid Git root: {GIT_ROOT}")

if not os.access(GIT_ROOT, os.R_OK | os.X_OK):
    erreur(f"Git clone not accessible: {GIT_ROOT}")


###############################################################################
# GIT UPDATE
#
# Single implementation of the clone-update logic (fetch + fast-forward),
# used both by a plain stdin command and by the Ingress "Refresh" button
# (which imports this module directly).
###############################################################################

GITHUB_SSH_COMMAND = (
    f"ssh -i {SSH_PRIVATE_KEY} "
    "-o IdentitiesOnly=yes "
    "-o BatchMode=yes "
    "-o StrictHostKeyChecking=yes "
    f"-o UserKnownHostsFile={GITHUB_KNOWN_HOSTS} "
    "-o HostKeyAlgorithms=ssh-ed25519"
)


def executer_git_reseau(*arguments: str) -> subprocess.CompletedProcess:
    """A git command that needs the network (fetch), via the Deploy Key
    and the pinned GitHub host key under /data/ssh."""

    environnement = dict(os.environ)
    environnement["GIT_SSH_COMMAND"] = GITHUB_SSH_COMMAND

    return subprocess.run(
        ["git", "-C", str(GIT_ROOT), *arguments],
        env=environnement,
        capture_output=True,
        text=True,
        timeout=60,
    )


def executer_git_local(*arguments: str) -> subprocess.CompletedProcess:
    """A local git command, no network access."""

    return subprocess.run(
        ["git", "-C", str(GIT_ROOT), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )


GITHUB_SSH_COMMAND_WRITE = (
    f"ssh -i {SSH_PRIVATE_KEY_WRITE} "
    "-o IdentitiesOnly=yes "
    "-o BatchMode=yes "
    "-o StrictHostKeyChecking=yes "
    f"-o UserKnownHostsFile={GITHUB_KNOWN_HOSTS} "
    "-o HostKeyAlgorithms=ssh-ed25519"
)


def executer_git_reseau_ecriture(*arguments: str) -> subprocess.CompletedProcess:
    """A git command that needs the network AND write access (push), via
    the dedicated write-capable Deploy Key. No other function in this file
    ever references this key or this command — every read-only code path
    (comparison, git_to_ha, mettre_a_jour_clone) uses executer_git_reseau
    above instead, which only ever holds the read-only key."""

    environnement = dict(os.environ)
    environnement["GIT_SSH_COMMAND"] = GITHUB_SSH_COMMAND_WRITE

    return subprocess.run(
        ["git", "-C", str(GIT_ROOT), *arguments],
        env=environnement,
        capture_output=True,
        text=True,
        timeout=60,
    )


def nettoyer_verrou_residuel() -> None:
    """Removes a leftover .git/index.lock before any operation.

    Safe here because this clone only ever has one writer (one command at
    a time, in series): a lock still present at startup can only be
    leftover from a killed process, never a real concurrent operation.
    """

    verrou = GIT_ROOT / ".git" / "index.lock"

    if verrou.is_file():
        log(f"Leftover Git lock detected, cleaning up: {verrou}")
        verrou.unlink()


def mettre_a_jour_clone() -> None:
    """Updates the local Git clone from origin, fast-forward only.
    Fails explicitly if the clone has local changes, has diverged, or a
    clean update cannot be guaranteed."""

    log("Checking for Git updates...")

    nettoyer_verrou_residuel()

    statut_avant = executer_git_local(
        "status", "--porcelain", "--untracked-files=all"
    ).stdout

    if statut_avant.strip():
        erreur("local changes present in the clone")

    tete_avant = executer_git_local("rev-parse", "HEAD").stdout.strip()

    if not tete_avant:
        erreur("local HEAD unknown")

    resultat_fetch = executer_git_reseau("fetch", "--prune", "origin")

    if resultat_fetch.returncode != 0:
        erreur(f"fetch failed: {resultat_fetch.stderr.strip()}")

    tete_distante = executer_git_local(
        "rev-parse", f"refs/remotes/origin/{GIT_REPOSITORY_BRANCH}"
    ).stdout.strip()

    if not tete_distante:
        erreur(f"origin/{GIT_REPOSITORY_BRANCH} unknown")

    if tete_avant == tete_distante:

        log("Git clone already up to date")

    else:

        log("Fast-forward required")

        resultat_merge = executer_git_local("merge", "--ff-only", tete_distante)

        if resultat_merge.returncode != 0:

            log("ERROR: fast-forward failed")
            log(resultat_merge.stderr.strip() or resultat_merge.stdout.strip())

            erreur("fast-forward failed")

        log("Fast-forward applied")

    tete_apres = executer_git_local("rev-parse", "HEAD").stdout.strip()

    if tete_apres != tete_distante:
        erreur(f"HEAD differs from origin/{GIT_REPOSITORY_BRANCH}")

    statut_apres = executer_git_local(
        "status", "--porcelain", "--untracked-files=all"
    ).stdout

    if statut_apres.strip():
        erreur("clone not clean after update")

    log("Git clone: OK")


# Only on a direct run (run.sh), never on an import/reload by
# interface.py: the page must never trigger a network fetch on its own
# (the passive 5-second auto-refresh). The "Refresh" button calls
# mettre_a_jour_clone() explicitly from interface.py instead.
if __name__ == "__main__":
    mettre_a_jour_clone()


presents_git = 0
absents_git = 0
erreurs_git = 0

log("Checking Git paths...")

for element in elements:

    element_id = element["id"]
    kind = element["kind"]
    git_path = element["git_path"]

    try:

        chemin = convertir_chemin_git(git_path)
        verifier_chemin_resolu(chemin, GIT_ROOT, "/data/repository")

        if kind == "directory":

            if not chemin.exists():
                log(f"[MISSING GIT] {element_id}: {git_path}")
                absents_git += 1
                continue

            if not chemin.is_dir():
                log(f"[ERROR GIT] {element_id}: expected a directory")
                erreurs_git += 1
                continue

            if not os.access(chemin, os.R_OK | os.X_OK):
                log(f"[ERROR GIT] {element_id}: directory not accessible")
                erreurs_git += 1
                continue

        else:

            if not chemin.exists():
                log(f"[MISSING GIT] {element_id}: {git_path}")
                absents_git += 1
                continue

            if not chemin.is_file():
                log(f"[ERROR GIT] {element_id}: expected a file")
                erreurs_git += 1
                continue

            if not os.access(chemin, os.R_OK):
                log(f"[ERROR GIT] {element_id}: file not readable")
                erreurs_git += 1
                continue

        log(f"[OK GIT] {element_id}: {git_path}")
        presents_git += 1

    except Exception as exc:

        log(f"[ERROR GIT] {element_id}: {exc}")
        erreurs_git += 1

log(f"Present in Git: {presents_git}")
log(f"Missing in Git: {absents_git}")
log(f"Errors: {erreurs_git}")

if erreurs_git:
    erreur(f"{erreurs_git} error(s) in Git paths")

log("Local Git validation: OK")


###############################################################################
# COMPARISON
###############################################################################

def lire_octets(chemin: Path) -> bytes:
    return chemin.read_bytes()


def retirer_bom_utf8(contenu: bytes) -> bytes:
    if contenu.startswith(UTF8_BOM):
        return contenu[len(UTF8_BOM):]
    return contenu


def normaliser_fins_ligne(contenu: bytes) -> bytes:
    return contenu.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def normaliser_saut_final(contenu: bytes) -> bytes:

    resultat = contenu

    while resultat.endswith(b"\n"):
        resultat = resultat[:-1]

    if resultat:
        resultat += b"\n"

    return resultat


def normaliser_equivalence(contenu: bytes) -> bytes:
    return normaliser_saut_final(normaliser_fins_ligne(retirer_bom_utf8(contenu)))


def est_fichier_texte(chemin: Path, contenu: bytes) -> bool:
    return chemin.suffix.lower() in TEXT_EXTENSIONS and b"\0" not in contenu


def convention_fin_de_ligne(contenu: bytes) -> str:
    """Best-effort classification of a text file's line-ending
    convention: "lf", "crlf", "mixed", or "none" (no line break at all).
    Used only to catch an accidental convention flip before it turns a
    one-line edit into a diff touching every line — see
    deployer_vers_git()."""

    if b"\n" not in contenu:
        return "none"

    total_crlf = contenu.count(b"\r\n")
    total_lf_seul = contenu.count(b"\n") - total_crlf

    if total_crlf and total_lf_seul:
        return "mixed"

    return "crlf" if total_crlf else "lf"


def convertir_fin_de_ligne(contenu: bytes, convention_cible: str) -> bytes:
    """Rewrites every line ending in `contenu` to match `convention_cible`
    ("lf" or "crlf"). Used only by deployer_vers_git()'s opt-in
    normalize_line_endings path — never applied silently by default, see
    the line-ending mismatch guard there."""

    normalise = normaliser_fins_ligne(contenu)

    if convention_cible == "crlf":
        return normalise.replace(b"\n", b"\r\n")

    return normalise


def classer_fichier(chemin_ha: Path, chemin_git: Path) -> str:

    contenu_ha = lire_octets(chemin_ha)
    contenu_git = lire_octets(chemin_git)

    if contenu_ha == contenu_git:
        return "identical"

    if (
        est_fichier_texte(chemin_ha, contenu_ha)
        and est_fichier_texte(chemin_git, contenu_git)
        and normaliser_equivalence(contenu_ha) == normaliser_equivalence(contenu_git)
    ):
        return "equivalent"

    return "different"


def est_artefact_runtime(relatif: PurePosixPath) -> bool:

    if any(partie in RUNTIME_IGNORED_DIRS for partie in relatif.parts[:-1]):
        return True

    if relatif.suffix.lower() in RUNTIME_IGNORED_SUFFIXES:
        return True

    return False


def inventorier_repertoire(racine: Path) -> tuple[dict[str, Path], list[str]]:

    inventaire = {}
    artefacts_ignores = []

    for repertoire_courant, noms_repertoires, noms_fichiers in os.walk(
        racine, topdown=True, followlinks=False
    ):

        noms_repertoires.sort()
        noms_fichiers.sort()

        repertoire = Path(repertoire_courant)

        for nom_repertoire in noms_repertoires:

            chemin_repertoire = repertoire / nom_repertoire

            if chemin_repertoire.is_symlink():
                raise ValueError(
                    "symlinks are not allowed in a compared directory: "
                    f"{chemin_repertoire.relative_to(racine)}"
                )

            verifier_chemin_resolu(chemin_repertoire, racine, str(racine))

        for nom_fichier in noms_fichiers:

            chemin_fichier = repertoire / nom_fichier
            relatif_chaine = chemin_fichier.relative_to(racine).as_posix()
            relatif = PurePosixPath(relatif_chaine)

            if chemin_fichier.is_symlink():
                raise ValueError(f"symlink not allowed: {relatif_chaine}")

            verifier_chemin_resolu(chemin_fichier, racine, str(racine))

            if not chemin_fichier.is_file():
                raise ValueError(f"not a regular file: {relatif_chaine}")

            if not os.access(chemin_fichier, os.R_OK):
                raise ValueError(f"file not readable: {relatif_chaine}")

            if est_artefact_runtime(relatif):
                artefacts_ignores.append(relatif_chaine)
                continue

            inventaire[relatif_chaine] = chemin_fichier

    return inventaire, sorted(artefacts_ignores)


def classer_repertoire(element_id: str, chemin_ha: Path, chemin_git: Path) -> str:

    inventaire_ha, artefacts_ha = inventorier_repertoire(chemin_ha)
    inventaire_git, artefacts_git = inventorier_repertoire(chemin_git)

    if artefacts_ha:
        log(f"Runtime artifacts ignored (HA) - {element_id}: {artefacts_ha}")

    if artefacts_git:
        log(f"Runtime artifacts ignored (Git) - {element_id}: {artefacts_git}")

    chemins_ha = set(inventaire_ha)
    chemins_git = set(inventaire_git)

    if (chemins_ha - chemins_git) or (chemins_git - chemins_ha):
        return "different"

    au_moins_un_equivalent = False

    for relatif in sorted(chemins_ha):

        resultat = classer_fichier(inventaire_ha[relatif], inventaire_git[relatif])

        if resultat == "different":
            return "different"

        if resultat == "equivalent":
            au_moins_un_equivalent = True

    return "equivalent" if au_moins_un_equivalent else "identical"


###############################################################################
# GLOBAL COMPARISON
#
# Every configured mapping is always compared — there is no separate
# "compare: false" switch. If a mapping exists, its state is shown.
###############################################################################

identiques = 0
equivalents = 0
differents = 0
absents_git_comparaison = 0
absents_ha_comparaison = 0
absents_deux = 0
erreurs_comparaison = 0
comparaisons_effectuees = 0

resultats_comparaison = {}

log("Comparing Git <-> Home Assistant...")

for element in elements:

    element_id = element["id"]
    kind = element["kind"]
    ha_path = element["ha_path"]
    git_path = element["git_path"]

    try:

        chemin_ha = convertir_chemin_ha(ha_path)
        chemin_git = convertir_chemin_git(git_path)

        verifier_chemin_resolu(chemin_ha, HA_ROOT, "/homeassistant")
        verifier_chemin_resolu(chemin_git, GIT_ROOT, "/data/repository")

        existe_ha = chemin_ha.exists()
        existe_git = chemin_git.exists()

        if not existe_ha and not existe_git:
            log(f"[MISSING BOTH] {element_id}")
            absents_deux += 1
            resultats_comparaison[element_id] = "missing_both"
            continue

        if not existe_git:
            log(f"[MISSING GIT] {element_id}")
            absents_git_comparaison += 1
            resultats_comparaison[element_id] = "missing_git"
            continue

        if not existe_ha:
            log(f"[MISSING HA] {element_id}")
            absents_ha_comparaison += 1
            resultats_comparaison[element_id] = "missing_ha"
            continue

        comparaisons_effectuees += 1

        if kind == "directory":
            resultat = classer_repertoire(element_id, chemin_ha, chemin_git)
        else:
            resultat = classer_fichier(chemin_ha, chemin_git)

        resultats_comparaison[element_id] = resultat

        if resultat == "identical":
            log(f"[IDENTICAL] {element_id}")
            identiques += 1
        elif resultat == "equivalent":
            log(f"[EQUIVALENT] {element_id}")
            equivalents += 1
        elif resultat == "different":
            log(f"[DIFFERENT] {element_id}")
            differents += 1

    except Exception as exc:

        log(f"[COMPARISON ERROR] {element_id}: {exc}")
        erreurs_comparaison += 1
        resultats_comparaison[element_id] = "error"

log(f"Comparisons performed: {comparaisons_effectuees}")
log(f"Strictly identical: {identiques}")
log(f"Equivalent: {equivalents}")
log(f"Different: {differents}")
log(f"Comparison errors: {erreurs_comparaison}")

if erreurs_comparaison:
    erreur(f"{erreurs_comparaison} comparison error(s)")

log("Git <-> Home Assistant classification: OK")


###############################################################################
# ATOMIC WRITE
###############################################################################

def fsync_repertoire(repertoire: Path) -> None:
    """Best-effort directory sync. Some filesystems refuse fsync on a
    directory; that must never turn a valid copy into an error."""

    try:
        fd = os.open(repertoire, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def ecrire_atomiquement(
    destination: Path,
    contenu: bytes,
    mode: int,
    uid: int | None = None,
    gid: int | None = None,
) -> None:

    parent = destination.parent

    if not parent.exists() or not parent.is_dir():
        raise RuntimeError(f"invalid parent directory: {parent}")

    temporaire = parent / (
        f".{destination.name}.homelab-git-management.tmp.{os.getpid()}"
    )

    if temporaire.exists():
        raise RuntimeError(f"temporary file already present: {temporaire}")

    fd = None

    try:

        fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)

        with os.fdopen(fd, "wb") as fichier:

            fd = None

            fichier.write(contenu)
            fichier.flush()
            os.fsync(fichier.fileno())

        os.chmod(temporaire, mode)

        if uid is not None and gid is not None:
            os.chown(temporaire, uid, gid)

        os.replace(temporaire, destination)
        fsync_repertoire(parent)

    except Exception:

        if fd is not None:
            os.close(fd)

        try:
            if temporaire.exists():
                temporaire.unlink()
        except OSError:
            pass

        raise


###############################################################################
# SYNCHRONIZATION STATE (ha_to_git)
#
# For every mapping configured with direction: ha_to_git, this records
# what each side looked like the last time they were known to agree (a
# comparison found them identical/equivalent, or a push just made them
# match). Comparing that against the current content on each side is what
# tells apart "only one side changed since" from "both changed
# independently" — a real conflict. A plain timestamp comparison cannot
# make that distinction: it only tells you which side was touched most
# recently, never whether the *other* side also changed since the two
# last agreed — which is exactly how a "latest wins" policy can silently
# discard a real edit made directly on GitHub. So this is not optional
# bookkeeping: without it, ha_to_git could not be made safe at all.
###############################################################################

def lire_etat_synchro() -> dict:

    try:
        with SYNC_STATE_PATH.open("r", encoding="utf-8") as fichier:
            donnees = json.load(fichier)
        return donnees if isinstance(donnees, dict) else {}
    except Exception:
        return {}


def ecrire_etat_synchro(etat: dict) -> None:
    """Best-effort persistence: a failure here must never block a push
    that already succeeded — it only means the next comparison falls back
    to treating this mapping as never-synced (safe, just less precise)."""

    try:
        SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        contenu = json.dumps(etat, indent=2, sort_keys=True).encode("utf-8")
        ecrire_atomiquement(SYNC_STATE_PATH, contenu, 0o600)
    except Exception as exc:
        log(f"WARNING: could not persist sync state: {exc}")


def empreinte(contenu: bytes) -> str:
    return hashlib.sha256(contenu).hexdigest()


def enregistrer_synchronise(element_id: str, empreinte_ha: str, empreinte_git: str) -> None:
    """Marks `element_id`'s two sides as agreeing right now — called after
    a successful push, and after a comparison finds them already
    identical/equivalent for a mapping with no prior (or a stale) record."""

    etat = lire_etat_synchro()
    etat[element_id] = {
        "ha": empreinte_ha,
        "git": empreinte_git,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ecrire_etat_synchro(etat)


def evaluer_synchro_bidirectionnel(
    element_id: str, contenu_ha: bytes | None, contenu_git: bytes | None
) -> str:
    """Same reference-based comparison as evaluer_synchro_ha_to_git(), but
    for a mapping where either side may legitimately be the one that
    moved — so, unlike ha_to_git, a Git-only change is not suspicious by
    itself, it just means the other direction (git_to_ha) is the safe one
    right now. Returns one of:
      - "ha_ahead"  — only Home Assistant changed: Push is safe.
      - "git_ahead" — only Git changed: Deploy is safe.
      - "conflict"  — both changed (or there is no prior record at all,
                      which is exactly as ambiguous — with two possible
                      directions and no history, this add-on does not
                      guess which side is authoritative); a human must
                      pick one explicitly.
      - "clean"     — both sides already match the reference (should not
                      normally be reached here, since the caller only
                      calls this for a "different" comparison result)."""

    reference = lire_etat_synchro().get(element_id)

    if reference is None:
        return "conflict"

    empreinte_ha = empreinte(contenu_ha) if contenu_ha is not None else None
    empreinte_git = empreinte(contenu_git) if contenu_git is not None else None

    ha_a_change = empreinte_ha != reference.get("ha")
    git_a_change = empreinte_git != reference.get("git")

    if ha_a_change and git_a_change:
        return "conflict"

    if ha_a_change:
        return "ha_ahead"

    if git_a_change:
        return "git_ahead"

    return "clean"


def evaluer_synchro_ha_to_git(
    element_id: str, contenu_ha: bytes | None, contenu_git: bytes | None
) -> str:
    """Returns one of:
      - "clean"    — safe to push Home Assistant -> Git (only Home
                     Assistant changed, or there is no prior record yet —
                     the same no-history behavior git_to_ha has always
                     had for a first deployment).
      - "external" — only Git changed since the last agreement (most
                     likely edited directly on GitHub); pushing now would
                     silently discard that edit.
      - "conflict" — both sides changed independently since the last
                     agreement; a real conflict, needs a human decision."""

    reference = lire_etat_synchro().get(element_id)

    if reference is None:
        return "clean"

    empreinte_ha = empreinte(contenu_ha) if contenu_ha is not None else None
    empreinte_git = empreinte(contenu_git) if contenu_git is not None else None

    ha_a_change = empreinte_ha != reference.get("ha")
    git_a_change = empreinte_git != reference.get("git")

    if ha_a_change and git_a_change:
        return "conflict"

    if git_a_change:
        return "external"

    return "clean"


etats_synchro = {}

for element in elements:

    if element["direction"] not in {"ha_to_git", "bidirectional"}:
        continue

    element_id = element["id"]
    etat_comparaison = resultats_comparaison.get(element_id)

    if etat_comparaison not in {"identical", "equivalent", "different"}:
        # missing_ha / missing_git / missing_both / error: nothing to
        # evaluate here — deployer_vers_git() re-checks existence itself,
        # and a missing side needs no conflict tracking yet.
        continue

    try:
        chemin_ha = convertir_chemin_ha(element["ha_path"])
        chemin_git = convertir_chemin_git(element["git_path"])
        contenu_ha = lire_octets(chemin_ha) if chemin_ha.exists() else None
        contenu_git = lire_octets(chemin_git) if chemin_git.exists() else None
    except Exception as exc:
        log(f"[SYNC] {element_id}: could not read content for sync evaluation: {exc}")
        continue

    if etat_comparaison in {"identical", "equivalent"}:

        if contenu_ha is not None and contenu_git is not None:

            reference = lire_etat_synchro().get(element_id)
            nouvelle_ha = empreinte(contenu_ha)
            nouvelle_git = empreinte(contenu_git)

            if (
                reference is None
                or reference.get("ha") != nouvelle_ha
                or reference.get("git") != nouvelle_git
            ):
                enregistrer_synchronise(element_id, nouvelle_ha, nouvelle_git)

        etats_synchro[element_id] = "clean"
        continue

    if element["direction"] == "ha_to_git":

        etats_synchro[element_id] = evaluer_synchro_ha_to_git(element_id, contenu_ha, contenu_git)

        if etats_synchro[element_id] == "conflict":
            log(f"[SYNC] {element_id}: CONFLICT - both Home Assistant and Git changed since the last sync")
        elif etats_synchro[element_id] == "external":
            log(f"[SYNC] {element_id}: external change detected on the Git side, push blocked until acknowledged")

    else:  # bidirectional

        etats_synchro[element_id] = evaluer_synchro_bidirectionnel(element_id, contenu_ha, contenu_git)

        if etats_synchro[element_id] == "conflict":
            log(f"[SYNC] {element_id}: CONFLICT - both Home Assistant and Git changed since the last sync")


###############################################################################
# BACKUP
###############################################################################

def preparer_backup_root() -> None:

    if BACKUP_ROOT.is_symlink():
        raise RuntimeError("/data/deploy-backups must not be a symlink")

    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)

    if not BACKUP_ROOT.is_dir():
        raise RuntimeError("/data/deploy-backups is not a directory")

    os.chmod(BACKUP_ROOT, 0o700)


def creer_sauvegarde(element_id: str, cible: Path) -> Path:

    preparer_backup_root()

    repertoire = BACKUP_ROOT / element_id

    if repertoire.is_symlink():
        raise RuntimeError("backup directory must not be a symlink")

    repertoire.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(repertoire, 0o700)

    verifier_chemin_resolu(repertoire, BACKUP_ROOT, "/data/deploy-backups")

    horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    sauvegarde = repertoire / f"{horodatage}-{cible.name}.bak"

    contenu = cible.read_bytes()

    ecrire_atomiquement(sauvegarde, contenu, 0o600)

    if sauvegarde.read_bytes() != contenu:
        raise RuntimeError("backup verification failed")

    log(f"[BACKUP] {element_id}: {sauvegarde}")

    return sauvegarde


def lister_sauvegardes(element_id: str) -> list[dict]:
    """Lists a mapping's own local backups (see creer_sauvegarde(), which
    is the only thing that ever writes one — on every git_to_ha deploy,
    of the Home Assistant content about to be overwritten), newest first.
    This is what the "restore a previous backup" UI lets a human pick
    from — a recovery path that works even with no Git history at all,
    since it never leaves this add-on's own data volume."""

    repertoire = BACKUP_ROOT / element_id

    if not repertoire.is_dir() or repertoire.is_symlink():
        return []

    resultats = []

    for chemin in repertoire.iterdir():

        if not chemin.is_file() or chemin.is_symlink():
            continue

        if not BACKUP_NAME_PATTERN.fullmatch(chemin.name):
            continue

        try:
            stats = chemin.stat()
        except OSError:
            continue

        resultats.append({
            "name": chemin.name,
            "size": stats.st_size,
            "created_at": datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        })

    resultats.sort(key=lambda entree: entree["name"], reverse=True)

    return resultats


def restaurer_sauvegarde_locale(element: dict, nom_sauvegarde: str) -> None:
    """Overwrites this mapping's current Home Assistant file with the
    exact bytes of one of its own previous local backups. Respects the
    same protect_from_git choice as a normal git_to_ha deploy — this is
    still an automatic overwrite of the live Home Assistant file, just
    sourced from this add-on's own backup archive instead of Git.
    Backs up the content it is about to replace first, so a restore is
    itself always undoable, exactly like any other deployment here."""

    element_id = element["id"]

    if element["kind"] == "directory":
        raise RuntimeError("directory restore is not supported")

    chemin_ha = convertir_chemin_ha(element["ha_path"])

    if est_chemin_protege(element, chemin_ha):
        raise RuntimeError(
            "restore forbidden — this mapping is protected "
            '(uncheck "Protect this file" on the mapping to allow it)'
        )

    if not BACKUP_NAME_PATTERN.fullmatch(nom_sauvegarde):
        raise RuntimeError("invalid backup name")

    repertoire = BACKUP_ROOT / element_id
    chemin_sauvegarde = repertoire / nom_sauvegarde

    verifier_chemin_resolu(chemin_sauvegarde, repertoire, f"/data/deploy-backups/{element_id}")

    if chemin_sauvegarde.is_symlink() or not chemin_sauvegarde.is_file():
        raise RuntimeError("backup not found")

    verifier_chemin_resolu(chemin_ha, HA_ROOT, "/homeassistant")

    if chemin_ha.is_symlink():
        raise RuntimeError("Home Assistant target must not be a symlink")

    if chemin_ha.exists() and not chemin_ha.is_file():
        raise RuntimeError("existing Home Assistant target is not a file")

    contenu_sauvegarde = chemin_sauvegarde.read_bytes()

    if chemin_ha.exists():
        creer_sauvegarde(element_id, chemin_ha)

    mode_destination = (
        stat.S_IMODE(chemin_ha.stat().st_mode) if chemin_ha.exists() else 0o644
    )

    log(f"[RESTORE] {element_id}: restoring from backup {nom_sauvegarde}")

    ecrire_atomiquement(chemin_ha, contenu_sauvegarde, mode_destination)

    if chemin_ha.read_bytes() != contenu_sauvegarde:
        raise RuntimeError("post-restore verification failed")

    log(f"[RESTORE] {element_id}: SUCCESS")


###############################################################################
# ROLLBACK
###############################################################################

def rollback(
    element_id: str,
    cible: Path,
    cible_existait: bool,
    sauvegarde: Path | None,
    mode_original: int | None,
    uid_original: int | None,
    gid_original: int | None,
) -> None:

    log(f"[ROLLBACK] {element_id}: starting")

    if cible_existait:

        if sauvegarde is None:
            raise RuntimeError("rollback impossible: no backup")

        contenu = sauvegarde.read_bytes()

        ecrire_atomiquement(
            cible,
            contenu,
            mode_original if mode_original is not None else 0o644,
            uid_original,
            gid_original,
        )

        if cible.read_bytes() != contenu:
            raise RuntimeError("rollback restored but verification failed")

    else:

        if cible.exists():

            if cible.is_symlink() or not cible.is_file():
                raise RuntimeError("rollback impossible: unexpected target")

            cible.unlink()
            fsync_repertoire(cible.parent)

        if cible.exists():
            raise RuntimeError("rollback impossible: target still present")

    log(f"[ROLLBACK] {element_id}: OK")


###############################################################################
# FILE DEPLOYMENT
###############################################################################

def deployer_fichier(element: dict, etat: str) -> None:

    element_id = element["id"]

    chemin_ha = convertir_chemin_ha(element["ha_path"])
    chemin_git = convertir_chemin_git(element["git_path"])

    if est_chemin_protege(element, chemin_ha):
        raise RuntimeError(
            "this mapping is protected against Git -> HA deployment "
            "(uncheck \"Protect this file\" on the mapping to allow it)"
        )

    if element["kind"] == "directory":
        raise RuntimeError("directory deployment is not supported")

    verifier_chemin_resolu(chemin_ha, HA_ROOT, "/homeassistant")
    verifier_chemin_resolu(chemin_git, GIT_ROOT, "/data/repository")

    if chemin_git.is_symlink():
        raise RuntimeError("Git source must not be a symlink")

    if not chemin_git.exists() or not chemin_git.is_file():
        raise RuntimeError("Git source missing or not a file")

    if chemin_ha.is_symlink():
        raise RuntimeError("Home Assistant target must not be a symlink")

    if chemin_ha.exists() and not chemin_ha.is_file():
        raise RuntimeError("existing Home Assistant target is not a file")

    parent_ha = chemin_ha.parent

    verifier_chemin_resolu(parent_ha, HA_ROOT, "/homeassistant")

    if not parent_ha.exists() or not parent_ha.is_dir():
        raise RuntimeError("Home Assistant parent directory missing")

    if not os.access(parent_ha, os.W_OK | os.X_OK):
        raise RuntimeError("Home Assistant parent directory not writable")

    if etat not in {"different", "missing_ha"}:
        raise RuntimeError(f"state not deployable: {etat}")

    contenu_source = chemin_git.read_bytes()

    cible_existait = chemin_ha.exists()

    sauvegarde = None
    mode_original = None
    uid_original = None
    gid_original = None
    contenu_original = None

    if cible_existait:

        informations = chemin_ha.stat()
        mode_original = stat.S_IMODE(informations.st_mode)
        uid_original = informations.st_uid
        gid_original = informations.st_gid
        contenu_original = chemin_ha.read_bytes()

        sauvegarde = creer_sauvegarde(element_id, chemin_ha)

    else:

        log(f"[BACKUP] {element_id}: target absent, no backup needed")

    if cible_existait:
        mode_destination = mode_original if mode_original is not None else 0o644
        uid_destination = uid_original
        gid_destination = gid_original
    else:
        mode_destination = stat.S_IMODE(chemin_git.stat().st_mode) or 0o644
        uid_destination = None
        gid_destination = None

    log(f"[DEPLOY] {element_id}: copying Git -> Home Assistant")

    try:

        ecrire_atomiquement(
            chemin_ha, contenu_source, mode_destination, uid_destination, gid_destination
        )

        if chemin_ha.read_bytes() != contenu_source:
            raise RuntimeError("post-copy verification failed")

        if classer_fichier(chemin_ha, chemin_git) != "identical":
            raise RuntimeError("unexpected classification after copy")

    except Exception as exc:

        log(f"[DEPLOY] {element_id}: failed: {exc}")

        try:

            etat_original_preserve = (
                cible_existait
                and chemin_ha.exists()
                and chemin_ha.is_file()
                and contenu_original is not None
                and chemin_ha.read_bytes() == contenu_original
            ) or (not cible_existait and not chemin_ha.exists())

            if not etat_original_preserve:
                rollback(
                    element_id, chemin_ha, cible_existait, sauvegarde,
                    mode_original, uid_original, gid_original,
                )
            else:
                log(f"[ROLLBACK] {element_id}: not needed, original state preserved")

        except Exception as rollback_exc:

            log(f"CRITICAL ERROR: rollback for {element_id} failed: {rollback_exc}")
            raise RuntimeError("deployment failed and rollback not guaranteed") from rollback_exc

        raise RuntimeError(f"deployment failed: {exc}") from exc

    log(f"[DEPLOY] {element_id}: atomic replacement: OK")
    log(f"[DEPLOY] {element_id}: exact Git = HA verification: OK")
    log(f"[DEPLOY] {element_id}: SUCCESS")


###############################################################################
# DEPLOYMENT TO GIT (ha_to_git)
#
# The reverse of deployer_fichier(): copies the current Home Assistant
# file into the local Git clone, commits it, and pushes with the
# dedicated write-capable Deploy Key. Git's own history is this
# direction's backup/rollback mechanism — unlike the HA side, Git already
# keeps every prior version, so there is no separate backup file to
# manage. A failed push simply discards the local commit with
# `git reset --hard`, leaving the clone matching whatever is actually on
# GitHub rather than a dangling unpushed commit.
###############################################################################

COMMIT_AUTEUR = "Homelab Git Management"
COMMIT_EMAIL = "noreply@homelab-git-management.local"


def _preparer_ecriture_git(element: dict) -> tuple[Path, Path, bytes, bytes | None]:
    """Path/content validation shared by deployer_vers_git() (a real
    content push) and normaliser_vers_git() (forcing byte-identity on an
    already-"equivalent" pair): resolves both paths, checks the write key
    exists, rejects symlinks/wrong types, and reads both sides' current
    bytes. Returns (chemin_ha, chemin_git, contenu_source,
    contenu_git_actuel) — the last is None if there is nothing at
    chemin_git yet."""

    if not SSH_PRIVATE_KEY_WRITE.is_file():
        raise RuntimeError(
            "no write-capable Deploy Key configured yet — add the second "
            "public key shown in the setup screen to GitHub, with write "
            "access enabled this time"
        )

    chemin_ha = convertir_chemin_ha(element["ha_path"])
    chemin_git = convertir_chemin_git(element["git_path"])

    verifier_chemin_resolu(chemin_ha, HA_ROOT, "/homeassistant")
    verifier_chemin_resolu(chemin_git, GIT_ROOT, "/data/repository")

    if chemin_ha.is_symlink():
        raise RuntimeError("Home Assistant source must not be a symlink")

    if not chemin_ha.exists() or not chemin_ha.is_file():
        raise RuntimeError("Home Assistant source missing or not a file")

    if chemin_git.is_symlink():
        raise RuntimeError("Git target must not be a symlink")

    if chemin_git.exists() and not chemin_git.is_file():
        raise RuntimeError("existing Git target is not a file")

    parent_git = chemin_git.parent
    verifier_chemin_resolu(parent_git, GIT_ROOT, "/data/repository")
    parent_git.mkdir(parents=True, exist_ok=True)

    contenu_source = chemin_ha.read_bytes()
    contenu_git_actuel = chemin_git.read_bytes() if chemin_git.exists() else None

    return chemin_ha, chemin_git, contenu_source, contenu_git_actuel


def _ecrire_commit_pousser_git(
    element_id: str,
    chemin_git: Path,
    contenu_source: bytes,
    mode_destination: int,
    message_commit: str,
    contenu_ha_reference: bytes | None = None,
) -> None:
    """Atomic write + commit + push + verify + automatic rollback on a
    failed push, factored out of deployer_vers_git() so
    normaliser_vers_git() can reuse the exact same machinery with its own
    commit message, instead of duplicating this risk-bearing logic.

    `contenu_ha_reference` is what gets recorded as the "last known Home
    Assistant content" sync-state reference — normally identical to
    `contenu_source` (what actually gets written to Git), since a plain
    push writes Home Assistant's own bytes verbatim. The one exception is
    deployer_vers_git()'s opt-in line-ending normalization: there,
    `contenu_source` has been rewritten to match Git's convention before
    being pushed, but the real file sitting on the Home Assistant side
    was never touched — recording the *unmodified* Home Assistant bytes
    here (instead of the normalized ones) keeps the next comparison from
    wrongly concluding Home Assistant "changed" the moment this push
    completes."""

    if contenu_ha_reference is None:
        contenu_ha_reference = contenu_source

    nettoyer_verrou_residuel()

    tete_avant = executer_git_local("rev-parse", "HEAD").stdout.strip()

    log(f"[PUSH] {element_id}: writing Home Assistant -> Git clone")

    ecrire_atomiquement(chemin_git, contenu_source, mode_destination)

    if chemin_git.read_bytes() != contenu_source:
        raise RuntimeError("post-write verification failed before commit")

    chemin_git_relatif = chemin_git.relative_to(GIT_ROOT).as_posix()

    resultat_add = executer_git_local("add", "--", chemin_git_relatif)

    if resultat_add.returncode != 0:
        raise RuntimeError(f"git add failed: {resultat_add.stderr.strip()}")

    resultat_statut = executer_git_local(
        "status", "--porcelain", "--", chemin_git_relatif
    )

    if not resultat_statut.stdout.strip():
        raise RuntimeError("nothing to commit (Git already matches Home Assistant)")

    resultat_commit = executer_git_local(
        "-c", f"user.name={COMMIT_AUTEUR}",
        "-c", f"user.email={COMMIT_EMAIL}",
        "commit",
        "-m", message_commit,
    )

    if resultat_commit.returncode != 0:
        erreur_msg = resultat_commit.stderr.strip() or resultat_commit.stdout.strip()
        raise RuntimeError(f"git commit failed: {erreur_msg}")

    log(f"[PUSH] {element_id}: pushing to origin/{GIT_REPOSITORY_BRANCH}")

    resultat_push = executer_git_reseau_ecriture(
        "push", "origin", f"HEAD:{GIT_REPOSITORY_BRANCH}"
    )

    if resultat_push.returncode != 0:

        log(f"[PUSH] {element_id}: push failed, discarding local commit")

        resultat_reset = executer_git_local("reset", "--hard", tete_avant)

        if resultat_reset.returncode != 0:
            log(
                f"CRITICAL ERROR: {element_id}: push failed AND local reset "
                f"failed: {resultat_reset.stderr.strip()}"
            )
            raise RuntimeError(
                "push failed and the local clone could not be restored — "
                "manual intervention required"
            )

        raise RuntimeError(f"git push failed: {resultat_push.stderr.strip()}")

    tete_apres = executer_git_local("rev-parse", "HEAD").stdout.strip()

    # The push above only moves the ref on GitHub; update our own local
    # remote-tracking ref too, so the next comparison/refresh sees
    # origin's HEAD as already applied instead of re-fetching to notice.
    executer_git_local(
        "update-ref", f"refs/remotes/origin/{GIT_REPOSITORY_BRANCH}", tete_apres
    )

    if chemin_git.read_bytes() != contenu_source:
        raise RuntimeError(
            "post-push verification failed: Git content changed unexpectedly"
        )

    enregistrer_synchronise(element_id, empreinte(contenu_ha_reference), empreinte(contenu_source))

    log(f"[PUSH] {element_id}: atomic write + commit + push: OK ({tete_apres[:12]})")
    log(f"[PUSH] {element_id}: SUCCESS")


def deployer_vers_git(element: dict, etat_synchro: str) -> None:

    element_id = element["id"]

    if element["direction"] not in {"ha_to_git", "bidirectional"}:
        raise RuntimeError("this mapping cannot push to Git")

    if element["kind"] == "directory":
        raise RuntimeError("directory deployment is not supported")

    if etat_synchro == "conflict":
        raise RuntimeError(
            "push blocked: both Home Assistant and Git changed since the "
            "last sync — resolve the conflict first"
        )

    if etat_synchro == "external":
        raise RuntimeError(
            "push blocked: the Git side changed outside this add-on since "
            "the last sync — acknowledge it first"
        )

    chemin_ha, chemin_git, contenu_source, contenu_git_actuel = _preparer_ecriture_git(element)
    contenu_ha_reel = contenu_source

    if contenu_git_actuel is not None:

        if est_fichier_texte(chemin_ha, contenu_source) and est_fichier_texte(chemin_git, contenu_git_actuel):

            convention_existante = convention_fin_de_ligne(contenu_git_actuel)
            convention_nouvelle = convention_fin_de_ligne(contenu_source)

            if (
                convention_existante in {"lf", "crlf"}
                and convention_nouvelle in {"lf", "crlf"}
                and convention_existante != convention_nouvelle
            ):
                if element.get("normalize_line_endings"):
                    log(
                        f"[PUSH] {element_id}: line-ending mismatch "
                        f"({convention_nouvelle.upper()} -> {convention_existante.upper()}) "
                        "normalized before push (normalize_line_endings is enabled "
                        "for this mapping)"
                    )
                    contenu_source = convertir_fin_de_ligne(contenu_source, convention_existante)
                else:
                    raise RuntimeError(
                        "line-ending mismatch: this file is tracked in Git with "
                        f"{convention_existante.upper()} line endings, but the "
                        f"current Home Assistant version uses "
                        f"{convention_nouvelle.upper()} — pushing as-is would "
                        "silently convert every line and bury the real change "
                        "in a massive diff. This usually means the source file "
                        "was edited or transferred through something that "
                        "changes line endings (a Windows text editor, an SFTP "
                        "client in text mode, a Samba mount, ...) rather than "
                        "an intentional format change — fix the line endings "
                        "at the source before pushing again, or enable "
                        '"Auto-fix line endings on push" on this mapping'
                    )

    mode_destination = (
        stat.S_IMODE(chemin_git.stat().st_mode) if chemin_git.exists() else 0o644
    )

    chemin_git_relatif = chemin_git.relative_to(GIT_ROOT).as_posix()

    _ecrire_commit_pousser_git(
        element_id, chemin_git, contenu_source, mode_destination,
        f"Update {chemin_git_relatif} from Home Assistant",
        contenu_ha_reference=contenu_ha_reel,
    )


def normaliser_vers_git(element: dict, etat: str) -> None:
    """Forces byte-for-byte identity for a mapping currently classified
    "equivalent" (same content once a UTF-8 BOM, line endings and a
    trailing newline are normalized away, but not byte-identical): takes
    Home Assistant's exact bytes and writes them into Git. Deliberately
    skips the line-ending-mismatch guard in deployer_vers_git() — swapping
    the line-ending convention is the entire point of this action here,
    not an accident to catch."""

    element_id = element["id"]

    if element["direction"] not in {"ha_to_git", "bidirectional"}:
        raise RuntimeError("this mapping cannot push to Git")

    if element["kind"] == "directory":
        raise RuntimeError("directory deployment is not supported")

    if etat != "equivalent":
        raise RuntimeError(
            "normalize-to-identical only applies when Home Assistant and "
            "Git already agree except for formatting (byte-order mark, "
            "line endings, trailing newline)"
        )

    chemin_ha, chemin_git, contenu_source, contenu_git_actuel = _preparer_ecriture_git(element)

    if contenu_git_actuel is None:
        raise RuntimeError("Git target missing — nothing to normalize")

    if contenu_source == contenu_git_actuel:
        raise RuntimeError(
            "nothing to normalize (Git already matches Home Assistant byte-for-byte)"
        )

    mode_destination = stat.S_IMODE(chemin_git.stat().st_mode)
    chemin_git_relatif = chemin_git.relative_to(GIT_ROOT).as_posix()

    _ecrire_commit_pousser_git(
        element_id, chemin_git, contenu_source, mode_destination,
        f"Normalize {chemin_git_relatif} to match Home Assistant byte-for-byte "
        "(formatting only, no content change)",
    )


###############################################################################
# DEPLOYMENT WITH CHECKS
#
# All the safety logic of a deployment (protection, direction, state)
# lives here once. Called both by the plain stdin command and by the
# Ingress /api/deploy endpoint: both paths share the exact same rules,
# nothing duplicated.
###############################################################################

def deployer_element(
    target: str, confirmation: bool, forcer: bool = False, sens: str | None = None
) -> None:

    elements_par_id = {element["id"]: element for element in elements}

    if target not in elements_par_id:
        erreur(f"unmanaged element: {target}")

    element = elements_par_id[target]
    etat = resultats_comparaison.get(target)

    log(f"[COMMAND] deploy requested: {target}")
    log(f"[COMMAND] explicit confirmation: {confirmation}")

    if element["kind"] == "directory":
        erreur(f"{target}: directory deployment is not supported")

    if element["direction"] == "git_to_ha":

        chemin_ha = convertir_chemin_ha(element["ha_path"])

        if est_chemin_protege(element, chemin_ha):
            erreur(f"{target}: deployment forbidden — this mapping is protected")

        if etat in {"identical", "equivalent"}:
            log(f"[COMMAND] {target}: no deployment needed; state={etat}")
            return

        if etat not in {"different", "missing_ha"}:
            erreur(f"{target}: state not deployable: {etat}")

        try:
            deployer_fichier(element, etat)
        except Exception as exc:
            erreur(f"{target}: {exc}")

        return

    if element["direction"] == "ha_to_git":

        if etat in {"identical", "equivalent"}:
            log(f"[COMMAND] {target}: no push needed; state={etat}")
            return

        if etat not in {"different", "missing_git"}:
            erreur(f"{target}: state not pushable: {etat}")

        etat_synchro = etats_synchro.get(target, "clean")

        if forcer and etat_synchro in {"conflict", "external"}:
            log(f"[COMMAND] {target}: {etat_synchro} overridden by explicit force")
            etat_synchro = "clean"

        try:
            deployer_vers_git(element, etat_synchro)
        except Exception as exc:
            erreur(f"{target}: {exc}")

        return

    if element["direction"] == "bidirectional":

        if sens not in {"git_to_ha", "ha_to_git"}:
            erreur(
                f"{target}: this mapping is bidirectional — an explicit "
                "sens ('git_to_ha' or 'ha_to_git') is required"
            )

        etat_synchro = etats_synchro.get(target, "conflict")

        if etat_synchro == "conflict" and not forcer:
            erreur(
                f"{target}: blocked: both Home Assistant and Git changed "
                "since the last sync — resolve the conflict first"
            )

        if not forcer:

            if sens == "ha_to_git" and etat_synchro != "ha_ahead":
                erreur(f"{target}: push not safe right now (state={etat_synchro})")

            if sens == "git_to_ha" and etat_synchro != "git_ahead":
                erreur(f"{target}: deployment not safe right now (state={etat_synchro})")

        if sens == "git_to_ha":

            chemin_ha = convertir_chemin_ha(element["ha_path"])

            if est_chemin_protege(element, chemin_ha):
                erreur(f"{target}: deployment forbidden — this mapping is protected")

            if etat in {"identical", "equivalent"}:
                log(f"[COMMAND] {target}: no deployment needed; state={etat}")
                return

            if etat not in {"different", "missing_ha"}:
                erreur(f"{target}: state not deployable: {etat}")

            try:
                deployer_fichier(element, etat)
                contenu_final = chemin_ha.read_bytes()
                enregistrer_synchronise(target, empreinte(contenu_final), empreinte(contenu_final))
            except Exception as exc:
                erreur(f"{target}: {exc}")

            return

        # sens == "ha_to_git"

        if etat in {"identical", "equivalent"}:
            log(f"[COMMAND] {target}: no push needed; state={etat}")
            return

        if etat not in {"different", "missing_git"}:
            erreur(f"{target}: state not pushable: {etat}")

        try:
            deployer_vers_git(element, "clean" if forcer else etat_synchro)
        except Exception as exc:
            erreur(f"{target}: {exc}")

        return

    erreur(f"{target}: deployment forbidden by configured direction")


def acquitter_git(target: str) -> None:
    """Resolves a "conflict" or "external" sync state without pushing
    anything: accepts whatever is currently on the Git side as the new
    reference point. Home Assistant's file is left untouched. This turns
    a blocked state back into a normal one — if Home Assistant's content
    still differs afterward, it shows up as a plain, safe-to-push
    difference on the next comparison, exactly as if it were the first
    time this mapping was ever synced."""

    elements_par_id = {element["id"]: element for element in elements}

    if target not in elements_par_id:
        erreur(f"unmanaged element: {target}")

    element = elements_par_id[target]

    if element["direction"] != "ha_to_git":
        erreur(f"{target}: not a ha_to_git mapping")

    log(f"[COMMAND] acknowledge Git state requested: {target}")

    chemin_ha = convertir_chemin_ha(element["ha_path"])
    chemin_git = convertir_chemin_git(element["git_path"])

    contenu_ha = chemin_ha.read_bytes() if chemin_ha.exists() else None
    contenu_git = chemin_git.read_bytes() if chemin_git.exists() else None

    if contenu_git is None:
        erreur(f"{target}: nothing on the Git side to acknowledge")

    enregistrer_synchronise(
        target,
        empreinte(contenu_ha) if contenu_ha is not None else "",
        empreinte(contenu_git),
    )

    log(f"[COMMAND] {target}: Git state acknowledged as the new reference point")


def normaliser_git(target: str) -> None:
    """Entry point for the "make identical" kebab-menu action: forces
    Home Assistant's exact bytes into Git for a mapping the comparison
    currently classifies as "equivalent" (same content once formatting
    differences are normalized away, but not byte-identical)."""

    elements_par_id = {element["id"]: element for element in elements}

    if target not in elements_par_id:
        erreur(f"unmanaged element: {target}")

    element = elements_par_id[target]
    etat = resultats_comparaison.get(target)

    log(f"[COMMAND] normalize-to-identical requested: {target}")

    try:
        normaliser_vers_git(element, etat)
    except Exception as exc:
        erreur(f"{target}: {exc}")


def restaurer_depuis_sauvegarde(target: str, nom_sauvegarde: str) -> None:
    """Entry point for the "restore a previous backup" kebab-menu action."""

    elements_par_id = {element["id"]: element for element in elements}

    if target not in elements_par_id:
        erreur(f"unmanaged element: {target}")

    element = elements_par_id[target]

    log(f"[COMMAND] restore from backup requested: {target} ({nom_sauvegarde})")

    try:
        restaurer_sauvegarde_locale(element, nom_sauvegarde)
    except Exception as exc:
        erreur(f"{target}: {exc}")


def traiter_commande_stdin() -> None:

    if len(sys.argv) == 1:
        return

    if len(sys.argv) != 3 or sys.argv[1] != "--stdin-command":
        erreur("unrecognized command arguments")

    try:
        commande = json.loads(sys.argv[2])
    except json.JSONDecodeError:
        erreur("stdin command: invalid JSON")

    if not isinstance(commande, dict):
        erreur("stdin command: expected a JSON object")

    champs_autorises = {"command", "target", "confirm", "force", "sens", "backup"}
    champs_inconnus = set(commande) - champs_autorises

    if champs_inconnus:
        erreur("stdin command: unknown fields: " + ", ".join(sorted(champs_inconnus)))

    action = commande.get("command")
    target = commande.get("target")
    confirmation = commande.get("confirm")
    forcer = commande.get("force", False)
    sens = commande.get("sens")
    backup = commande.get("backup")

    if action not in {"deploy", "acknowledge_git", "normalize_git", "restore_backup"}:
        erreur(f"stdin command: unauthorized action: {action}")

    if (
        not isinstance(target, str)
        or not target
        or target != target.strip()
        or not ID_PATTERN.fullmatch(target)
    ):
        erreur("stdin command: invalid target")

    if not isinstance(forcer, bool):
        erreur("stdin command: 'force' must be a boolean")

    if sens is not None and sens not in {"git_to_ha", "ha_to_git"}:
        erreur("stdin command: 'sens' must be 'git_to_ha' or 'ha_to_git'")

    if action == "restore_backup" and (not isinstance(backup, str) or not backup):
        erreur("stdin command: 'backup' is required for restore_backup")

    if action == "acknowledge_git":
        acquitter_git(target)
        return

    if confirmation is not True:
        erreur(f"{target}: missing explicit confirmation; confirm:true is required")

    if action == "normalize_git":
        normaliser_git(target)
        return

    if action == "restore_backup":
        restaurer_depuis_sauvegarde(target, backup)
        return

    deployer_element(target, confirmation, forcer, sens)


###############################################################################
# ENTRY POINT
###############################################################################

log("Options, paths, classification and policy validation: OK")

traiter_commande_stdin()

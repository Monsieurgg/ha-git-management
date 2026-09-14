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

BACKUP_ROOT = Path("/data/deploy-backups")

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

# Only this direction has a real implementation so far. ha_to_git and
# bidirectional both need a write-capable Deploy Key and a commit/push
# path that do not exist yet — accepted by the schema, refused here with
# an explicit message rather than silently doing nothing.
IMPLEMENTED_DIRECTIONS = {"git_to_ha"}

ALLOWED_KINDS = {"file", "directory"}

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Home Assistant's core YAML files are always protected against Git -> HA
# writes, no matter what a mapping declares. This is independent of the
# options a user configures: a mistake in the mappings list must never be
# able to overwrite these.
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
            "this version (only git_to_ha is supported today) — remove "
            "this mapping or set direction: git_to_ha"
        )

    ha_path = mapping.get("ha_path")
    git_path = mapping.get("git_path")

    if not isinstance(ha_path, str) or not ha_path.strip():
        erreur(f"{element_id}: invalid ha_path")

    if not isinstance(git_path, str) or not git_path.strip():
        erreur(f"{element_id}: invalid git_path")

    elements.append(
        {
            "id": element_id,
            "kind": kind,
            "direction": direction,
            "ha_path": ha_path,
            "git_path": git_path,
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


def est_chemin_protege(chemin_ha: Path) -> bool:
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

    if est_chemin_protege(chemin_ha):
        raise RuntimeError(
            "this file is a core Home Assistant file and is always "
            "protected against Git -> HA deployment"
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
# DEPLOYMENT WITH CHECKS
#
# All the safety logic of a deployment (protection, direction, state)
# lives here once. Called both by the plain stdin command and by the
# Ingress /api/deploy endpoint: both paths share the exact same rules,
# nothing duplicated.
###############################################################################

def deployer_element(target: str, confirmation: bool) -> None:

    elements_par_id = {element["id"]: element for element in elements}

    if target not in elements_par_id:
        erreur(f"unmanaged element: {target}")

    element = elements_par_id[target]
    etat = resultats_comparaison.get(target)

    log(f"[COMMAND] deploy requested: {target}")
    log(f"[COMMAND] explicit confirmation: {confirmation}")

    chemin_ha = convertir_chemin_ha(element["ha_path"])

    if est_chemin_protege(chemin_ha):
        erreur(f"{target}: deployment forbidden — protected core file")

    if element["direction"] != "git_to_ha":
        erreur(f"{target}: deployment forbidden by configured direction")

    if element["kind"] == "directory":
        erreur(f"{target}: directory deployment is not supported")

    if etat in {"identical", "equivalent"}:
        log(f"[COMMAND] {target}: no deployment needed; state={etat}")
        return

    if etat not in {"different", "missing_ha"}:
        erreur(f"{target}: state not deployable: {etat}")

    try:
        deployer_fichier(element, etat)
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

    champs_autorises = {"command", "target", "confirm"}
    champs_inconnus = set(commande) - champs_autorises

    if champs_inconnus:
        erreur("stdin command: unknown fields: " + ", ".join(sorted(champs_inconnus)))

    action = commande.get("command")
    target = commande.get("target")
    confirmation = commande.get("confirm")

    if action != "deploy":
        erreur(f"stdin command: unauthorized action: {action}")

    if (
        not isinstance(target, str)
        or not target
        or target != target.strip()
        or not ID_PATTERN.fullmatch(target)
    ):
        erreur("stdin command: invalid target")

    if confirmation is not True:
        erreur(f"{target}: missing explicit confirmation; confirm:true is required")

    deployer_element(target, confirmation)


###############################################################################
# ENTRY POINT
###############################################################################

log("Options, paths, classification and policy validation: OK")

traiter_commande_stdin()

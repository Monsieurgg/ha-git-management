#!/usr/bin/with-contenv bashio

# =============================================================================
# HOMELAB GIT MANAGEMENT — RUN.SH
# =============================================================================
#
# Role:
#   - generate the Deploy Key on first boot and always make it available
#     to the Ingress UI (the setup screen shows it even before the
#     repository is configured, so a user can paste it into GitHub);
#   - read github_repository / github_branch from the add-on options;
#   - if a repository is configured: pin GitHub's host key, verify
#     read-only access, clone (first run) or hand updates to
#     gestionnaire.py's own update engine;
#   - if no repository is configured yet: skip all Git operations and
#     start the Ingress UI in setup mode so the user can read the public
#     key and paste it into GitHub -> Settings -> Deploy Keys;
#   - keep the stdin command channel open;
#   - start the Ingress interface.
#
# Security:
#   - both private Deploy Keys only ever live under /data/ssh, a
#     persistent volume outside the image, generated at runtime — never
#     baked into the image, never transmitted anywhere by this add-on;
#   - only the *public* halves are ever displayed (Ingress UI and logs);
#   - StrictHostKeyChecking is enforced, GitHub's host key is pinned, for
#     both keys;
#   - the first (read-only) key is used by every comparison and by
#     git_to_ha; the second, entirely separate key is the only one ever
#     used to push (ha_to_git) — read-only code paths never reference it,
#     and it grants nothing until explicitly added to GitHub with write
#     access enabled;
#   - fast-forward only Git updates; a modified local clone is refused;
#   - all deployments to Home Assistant remain the exclusive
#     responsibility of gestionnaire.py, gated on user confirmation.
#
# =============================================================================

set -e

APP_VERSION="1.1.0"

export APP_VERSION

echo "[homelab-git-management] Starting"
echo "[homelab-git-management] Application: ${APP_VERSION}"
echo "[homelab-git-management] GitHub write access: only via a separate write key, only for mappings explicitly configured with ha_to_git"
echo "[homelab-git-management] Home Assistant write access: only on explicit confirmation"

###############################################################################
# TOOLS
###############################################################################

echo "[homelab-git-management] Checking tools..."

for tool in git ssh ssh-keygen python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        bashio::exit.nok "Required tool missing: ${tool}"
    fi
done

echo "[homelab-git-management] Tools: OK"

###############################################################################
# /data
###############################################################################

DATA_DIR="/data"

DATA_TEST_FILE="${DATA_DIR}/.homelab-git-management-write-test.$$"

echo "[homelab-git-management] Checking /data..."

if [ ! -d "$DATA_DIR" ] || [ ! -w "$DATA_DIR" ]; then
    bashio::exit.nok "/data missing or not writable"
fi

if ! : > "$DATA_TEST_FILE" || ! rm -f "$DATA_TEST_FILE"; then
    bashio::exit.nok "/data write test failed"
fi

echo "[homelab-git-management] /data: OK"

###############################################################################
# HOME ASSISTANT MOUNT
#
# No specific file is assumed to exist here — that would only make sense
# for one particular user's configuration. We only check that the mount
# itself is usable; gestionnaire.py checks each individually configured
# ha_path when it loads the user's mappings.
###############################################################################

if [ ! -d /homeassistant ] || [ ! -r /homeassistant ] || [ ! -w /homeassistant ]; then
    bashio::exit.nok "/homeassistant mount missing, unreadable or read-only"
fi

echo "[homelab-git-management] Home Assistant mount: OK"

###############################################################################
# SSH / DEPLOY KEY
#
# Generated unconditionally on first boot, independent of whether a
# repository is configured yet: the Ingress setup screen needs the
# public key to exist so it can be displayed to the user.
###############################################################################

SSH_DIR="${DATA_DIR}/ssh"

if [ -L "$SSH_DIR" ]; then
    bashio::exit.nok "/data/ssh must not be a symlink"
fi

mkdir -p "$SSH_DIR"
chown root:root "$SSH_DIR"
chmod 700 "$SSH_DIR"

SSH_PRIVATE_KEY="${SSH_DIR}/github_deploy_key"
SSH_PUBLIC_KEY="${SSH_PRIVATE_KEY}.pub"

if [ -L "$SSH_PRIVATE_KEY" ] || [ -L "$SSH_PUBLIC_KEY" ]; then
    bashio::exit.nok "SSH key files must not be symlinks"
fi

if [ ! -e "$SSH_PRIVATE_KEY" ]; then

    echo "[homelab-git-management] Generating Deploy Key..."

    (
        umask 077
        ssh-keygen -q -t ed25519 -N "" -C "homelab-git-management" -f "$SSH_PRIVATE_KEY"
    )

fi

chown root:root "$SSH_PRIVATE_KEY" "$SSH_PUBLIC_KEY"
chmod 600 "$SSH_PRIVATE_KEY"
chmod 644 "$SSH_PUBLIC_KEY"

if ! ssh-keygen -y -f "$SSH_PRIVATE_KEY" >/dev/null 2>&1; then
    bashio::exit.nok "Invalid Deploy Key private key"
fi

echo "[homelab-git-management] Deploy Key: OK (public key available in the Ingress UI)"

###############################################################################
# WRITE-CAPABLE DEPLOY KEY (ha_to_git)
#
# A second, entirely separate keypair — never used by any read-only code
# path (comparison, git_to_ha, the initial GitHub access test just below).
# Generated unconditionally like the one above, so the setup screen can
# always show its public half, but it grants no access at all until a
# user explicitly adds it on GitHub with write access enabled: unlike the
# key above, this one is deliberately NOT added automatically to anything
# — ha_to_git mappings simply fail with a clear error until it is.
###############################################################################

SSH_PRIVATE_KEY_WRITE="${SSH_DIR}/github_deploy_key_write"
SSH_PUBLIC_KEY_WRITE="${SSH_PRIVATE_KEY_WRITE}.pub"

if [ -L "$SSH_PRIVATE_KEY_WRITE" ] || [ -L "$SSH_PUBLIC_KEY_WRITE" ]; then
    bashio::exit.nok "Write Deploy Key files must not be symlinks"
fi

if [ ! -e "$SSH_PRIVATE_KEY_WRITE" ]; then

    echo "[homelab-git-management] Generating write-capable Deploy Key (for ha_to_git)..."

    (
        umask 077
        ssh-keygen -q -t ed25519 -N "" -C "homelab-git-management-write" -f "$SSH_PRIVATE_KEY_WRITE"
    )

fi

chown root:root "$SSH_PRIVATE_KEY_WRITE" "$SSH_PUBLIC_KEY_WRITE"
chmod 600 "$SSH_PRIVATE_KEY_WRITE"
chmod 644 "$SSH_PUBLIC_KEY_WRITE"

if ! ssh-keygen -y -f "$SSH_PRIVATE_KEY_WRITE" >/dev/null 2>&1; then
    bashio::exit.nok "Invalid write Deploy Key private key"
fi

echo "[homelab-git-management] Write Deploy Key: OK (inactive until added to GitHub with write access — see the setup screen)"

###############################################################################
# OPTIONS
###############################################################################

GITHUB_REPOSITORY_OPTION="$(bashio::config 'github_repository')"
GITHUB_BRANCH="$(bashio::config 'github_branch')"

if [ -z "$GITHUB_BRANCH" ] || [ "$GITHUB_BRANCH" = "null" ]; then
    GITHUB_BRANCH="main"
fi

# Defense in depth: even though only this add-on's own admin sets this
# option, a branch name is still about to be passed as a command-line
# argument to git — reject anything that could be misread as a flag
# (leading "-") or that is not a plausible branch name.
if ! printf '%s' "$GITHUB_BRANCH" | grep -Eq '^[A-Za-z0-9_./-]+$' || \
   [ "${GITHUB_BRANCH#-}" != "$GITHUB_BRANCH" ]; then
    bashio::exit.nok "github_branch is not a valid branch name: ${GITHUB_BRANCH}"
fi

if [ -z "$GITHUB_REPOSITORY_OPTION" ] || [ "$GITHUB_REPOSITORY_OPTION" = "null" ]; then

    echo "[homelab-git-management] No repository configured yet."
    echo "[homelab-git-management] Open the add-on's web UI, copy the public key shown there,"
    echo "[homelab-git-management] add it as a read-only Deploy Key on your GitHub repository,"
    echo "[homelab-git-management] then set github_repository / github_branch in the add-on options."

    REPOSITORY_CONFIGURED=false

elif ! printf '%s' "$GITHUB_REPOSITORY_OPTION" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then

    bashio::exit.nok "github_repository must look like 'owner/repository'"

else

    REPOSITORY_CONFIGURED=true
    GITHUB_REPOSITORY="git@github.com:${GITHUB_REPOSITORY_OPTION}.git"

fi

###############################################################################
# GITHUB IDENTITY + CLONE (only if a repository is configured)
###############################################################################

if [ "$REPOSITORY_CONFIGURED" = true ]; then

    GITHUB_KNOWN_HOSTS="${SSH_DIR}/known_hosts"

    GITHUB_HOST_KEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    GITHUB_HOST_FINGERPRINT_EXPECTED="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

    if [ -L "$GITHUB_KNOWN_HOSTS" ]; then
        bashio::exit.nok "known_hosts must not be a symlink"
    fi

    printf '%s\n' "$GITHUB_HOST_KEY" > "$GITHUB_KNOWN_HOSTS"
    chown root:root "$GITHUB_KNOWN_HOSTS"
    chmod 644 "$GITHUB_KNOWN_HOSTS"

    GITHUB_HOST_FINGERPRINT_ACTUAL="$(ssh-keygen -E sha256 -l -f "$GITHUB_KNOWN_HOSTS" | awk '{print $2}')"

    if [ "$GITHUB_HOST_FINGERPRINT_ACTUAL" != "$GITHUB_HOST_FINGERPRINT_EXPECTED" ]; then
        bashio::exit.nok "Unexpected GitHub host key fingerprint"
    fi

    GITHUB_SSH_COMMAND="ssh \
-i ${SSH_PRIVATE_KEY} \
-o IdentitiesOnly=yes \
-o BatchMode=yes \
-o StrictHostKeyChecking=yes \
-o UserKnownHostsFile=${GITHUB_KNOWN_HOSTS} \
-o HostKeyAlgorithms=ssh-ed25519"

    echo "[homelab-git-management] Testing GitHub access..."

    if ! GIT_SSH_COMMAND="$GITHUB_SSH_COMMAND" git ls-remote "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
        bashio::exit.nok "Cannot reach GitHub. Make sure the public key shown in the Ingress UI was added as a Deploy Key on ${GITHUB_REPOSITORY_OPTION}."
    fi

    echo "[homelab-git-management] GitHub read access: OK"

    GIT_REPOSITORY_DIR="${DATA_DIR}/repository"

    if [ -L "$GIT_REPOSITORY_DIR" ]; then
        bashio::exit.nok "repository clone path must not be a symlink"
    fi

    if [ ! -e "$GIT_REPOSITORY_DIR" ]; then

        GIT_CLONE_TMP="${DATA_DIR}/.repository-clone.$$"

        if [ -e "$GIT_CLONE_TMP" ]; then
            bashio::exit.nok "temporary clone path already exists"
        fi

        if ! GIT_SSH_COMMAND="$GITHUB_SSH_COMMAND" git clone \
                --branch "$GITHUB_BRANCH" \
                --single-branch \
                --depth 1 \
                "$GITHUB_REPOSITORY" \
                "$GIT_CLONE_TMP"; then

            rm -rf "$GIT_CLONE_TMP"
            bashio::exit.nok "Clone failed"

        fi

        mv "$GIT_CLONE_TMP" "$GIT_REPOSITORY_DIR"

    fi

    if [ ! -d "${GIT_REPOSITORY_DIR}/.git" ]; then
        bashio::exit.nok "Invalid Git repository at ${GIT_REPOSITORY_DIR}"
    fi

    GIT_ORIGIN="$(git -C "$GIT_REPOSITORY_DIR" remote get-url origin 2>/dev/null || true)"

    if [ "$GIT_ORIGIN" != "$GITHUB_REPOSITORY" ]; then
        bashio::exit.nok "Unexpected Git origin (repository option changed?): ${GIT_ORIGIN}"
    fi

    GIT_CURRENT_BRANCH="$(git -C "$GIT_REPOSITORY_DIR" branch --show-current 2>/dev/null || true)"

    if [ "$GIT_CURRENT_BRANCH" != "$GITHUB_BRANCH" ]; then
        bashio::exit.nok "Unexpected current branch: ${GIT_CURRENT_BRANCH}"
    fi

    echo "[homelab-git-management] Running the engine's initial validation..."

    python3 /app/gestionnaire.py

    echo "[homelab-git-management] Engine: OK"

else

    echo "[homelab-git-management] Skipping Git setup: waiting for configuration."

fi

###############################################################################
# INGRESS INTERFACE
###############################################################################

INTERFACE_PID=""

stop_interface() {
    if [ -n "$INTERFACE_PID" ] && kill -0 "$INTERFACE_PID" >/dev/null 2>&1; then
        kill "$INTERFACE_PID" >/dev/null 2>&1 || true
        wait "$INTERFACE_PID" 2>/dev/null || true
    fi
}

trap stop_interface EXIT INT TERM

echo "[homelab-git-management] Starting Ingress interface..."

python3 /app/interface.py &

INTERFACE_PID="$!"

sleep 1

if ! kill -0 "$INTERFACE_PID" >/dev/null 2>&1; then
    echo "[homelab-git-management] ERROR: Ingress interface stopped on startup"
    wait "$INTERFACE_PID" || true
    exit 1
fi

echo "[homelab-git-management] Ingress interface: ready"

###############################################################################
# STDIN
###############################################################################

echo "[homelab-git-management] stdin command channel: ready"
echo "[homelab-git-management] Startup complete (${APP_VERSION})"

while true; do

    if IFS= read -r command_json; then

        if [ -z "$command_json" ]; then
            continue
        fi

        if [ "${#command_json}" -gt 4096 ]; then
            echo "[homelab-git-management] ERROR: stdin command too long"
            continue
        fi

        echo "[homelab-git-management] stdin command received"

        if [ "$REPOSITORY_CONFIGURED" != true ]; then
            echo "[homelab-git-management] Command rejected: no repository configured"
            continue
        fi

        if ! python3 /app/gestionnaire.py --stdin-command "$command_json"; then
            echo "[homelab-git-management] stdin command refused or failed"
        fi

    else
        sleep 1
    fi

done

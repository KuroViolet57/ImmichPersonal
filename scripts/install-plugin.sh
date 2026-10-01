#!/usr/bin/env bash
#
# Installs the Smart Album plugin into a Docker Compose Immich deployment.
#
# Run this on the machine where Immich runs -- inside WSL2, not PowerShell.
# (From Windows, scripts/Install-ImmichPlugin.ps1 calls this for you.)
#
#   ./scripts/install-plugin.sh [options] [/path/to/folder/with/docker-compose.yml]
#
# Options:
#   --all          Do everything: files, .env, the docker-compose.yml volume,
#                  restart, and verify. Without it the compose edit is only
#                  printed for you to make by hand.
#   --no-restart   With --all, make every change but leave the stack alone.
#   -y, --yes      Do not prompt before changing .env or docker-compose.yml.
#   -h, --help     Show this.
#
# Safety: every file is backed up before it is touched, and a docker-compose.yml
# that fails `docker compose config` after editing is restored automatically.
set -euo pipefail

PLUGIN_DIR_NAME="immich-smart-album"
RAW_BASE="https://raw.githubusercontent.com/KuroViolet57/ImmichPersonal/claude/immich-album-organization-2sj7f8/plugins/immich-smart-album"
ANCHOR='- /etc/localtime:/etc/localtime:ro'

ASSUME_YES=0
DO_ALL=0
DO_RESTART=1
COMPOSE_DIR=""
CHANGED=()

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '  %s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)     ASSUME_YES=1; shift ;;
    --all)        DO_ALL=1; shift ;;
    --no-restart) DO_RESTART=0; shift ;;
    -h|--help)    sed -n '3,20p' "$0"; exit 0 ;;
    -*)           die "unknown option: $1" ;;
    *)            COMPOSE_DIR="$1"; shift ;;
  esac
done

# ---------------------------------------------------------------- compose dir

find_compose_dir() {
  # Ask Docker where the running stack was started from before guessing.
  local from_docker
  from_docker="$(docker inspect immich_server \
    --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  if [ -n "$from_docker" ] && [ -f "$from_docker/docker-compose.yml" ]; then
    printf '%s' "$from_docker"; return 0
  fi
  for candidate in "$HOME/immich-app" "$HOME/immich" "$HOME/docker/immich" \
                   /opt/immich /srv/immich "$PWD"; do
    if [ -f "$candidate/docker-compose.yml" ] \
       && grep -q 'immich-server' "$candidate/docker-compose.yml" 2>/dev/null; then
      printf '%s' "$candidate"; return 0
    fi
  done
  return 1
}

step "Locating your Immich deployment"
if [ -z "$COMPOSE_DIR" ]; then
  COMPOSE_DIR="$(find_compose_dir)" \
    || die "could not find docker-compose.yml. Pass the folder: $0 /path/to/immich"
fi
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd)"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="$COMPOSE_DIR/.env"
[ -f "$COMPOSE_FILE" ] || die "no docker-compose.yml in $COMPOSE_DIR"
grep -q 'immich-server' "$COMPOSE_FILE" || die "$COMPOSE_FILE is not an Immich compose file"
[ -f "$ENV_FILE" ] || die "no .env in $COMPOSE_DIR (Immich reads its settings from it)"
say "  $COMPOSE_FILE"

# ------------------------------------------------------- does Immich support it

step "Checking this Immich has Workflows"
# /api/plugins is auth-guarded: 401 means the route exists, 404 means it does not.
# curl already writes 000 via -w when it cannot connect, so do not add another.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  http://localhost:2283/api/plugins 2>/dev/null || true)"
[ -n "$code" ] || code="000"
case "$code" in
  401|403) say "  supported (HTTP $code from /api/plugins)" ;;
  404) die "this Immich predates Workflows, so the plugin cannot load. Update Immich first." ;;
  000) warn "could not reach Immich on localhost:2283 -- continuing, but verify yourself afterwards" ;;
  *)   warn "unexpected HTTP $code from /api/plugins -- continuing" ;;
esac

# ------------------------------------------------------------- plugin payload

step "Installing the plugin files"
SOURCE_DIR="$(cd "$(dirname "$0")/../plugins/$PLUGIN_DIR_NAME" 2>/dev/null && pwd || true)"
TARGET_DIR="$COMPOSE_DIR/plugins/$PLUGIN_DIR_NAME"
mkdir -p "$TARGET_DIR"

if [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/manifest.json" ] && [ -f "$SOURCE_DIR/dist/plugin.wasm" ]; then
  say "  using the copy in this repository"
  cp "$SOURCE_DIR/manifest.json" "$TARGET_DIR/manifest.json"
  cp "$SOURCE_DIR/dist/plugin.wasm" "$TARGET_DIR/plugin.wasm"
else
  say "  downloading from GitHub"
  command -v curl >/dev/null || die "curl is required"
  curl -fsSL -o "$TARGET_DIR/manifest.json" "$RAW_BASE/manifest.json"
  curl -fsSL -o "$TARGET_DIR/plugin.wasm" "$RAW_BASE/dist/plugin.wasm"
fi

# The manifest names the wasm file; a mismatch produces a confusing read error
# at boot rather than a validation failure, so check it here instead.
WASM_NAME="$(sed -n 's/.*"wasmPath"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TARGET_DIR/manifest.json")"
[ "$WASM_NAME" = "plugin.wasm" ] || die "manifest wasmPath is '$WASM_NAME' but the file is plugin.wasm"
# WebAssembly magic number: a truncated download or an HTML error page fails here.
head -c 4 "$TARGET_DIR/plugin.wasm" | od -An -tx1 | grep -q '00 61 73 6d' \
  || die "plugin.wasm is not a WebAssembly module (download failed?)"
say "  $TARGET_DIR  ($(wc -c < "$TARGET_DIR/plugin.wasm") bytes)"
CHANGED+=("$TARGET_DIR/{manifest.json,plugin.wasm}")

# ---------------------------------------------------------------------- .env

step "Enabling external plugins in .env"
needed=""
grep -q '^[[:space:]]*IMMICH_ALLOW_EXTERNAL_PLUGINS=' "$ENV_FILE" \
  || needed="${needed}IMMICH_ALLOW_EXTERNAL_PLUGINS=true"$'\n'
grep -q '^[[:space:]]*IMMICH_PLUGINS_INSTALL_FOLDER=' "$ENV_FILE" \
  || needed="${needed}IMMICH_PLUGINS_INSTALL_FOLDER=/plugins"$'\n'

if [ -z "$needed" ]; then
  say "  already set, leaving .env alone"
else
  printf '  adding:\n'; printf '    %s\n' $needed
  confirm "Append to $ENV_FILE?" || die "stopped; add those lines yourself and re-run"
  backup="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
  cp "$ENV_FILE" "$backup"
  { printf '\n# Added by immich-organizer install-plugin.sh\n'; printf '%s' "$needed"; } >> "$ENV_FILE"
  say "  done (backup: $backup)"
  CHANGED+=("$ENV_FILE  (backup: $backup)")
fi

# ------------------------------------------------------------ compose volume

step "Mounting the plugins folder"
if grep -qE '^[[:space:]]*-[[:space:]]*\./plugins:/plugins' "$COMPOSE_FILE"; then
  say "  already mounted"
  MOUNT_OK=1
elif [ "$DO_ALL" -eq 0 ]; then
  MOUNT_OK=0
  cat <<EOF
  Not editing docker-compose.yml without --all. Add this line yourself, under
  the 'volumes:' list of the immich-server service, matching its indentation:

      - ./plugins:/plugins

  in: $COMPOSE_FILE
EOF
else
  # -F and -- matter: the anchor text starts with '-', which grep would
  # otherwise read as an option.
  anchors="$(grep -cF -- "$ANCHOR" "$COMPOSE_FILE" || true)"
  [ -n "$anchors" ] || anchors=0
  [ "$anchors" = "1" ] || die \
    "expected exactly one '$ANCHOR' line to anchor the edit, found $anchors. Your compose file is customised -- add '- ./plugins:/plugins' to immich-server's volumes by hand."

  confirm "Add the volume line to $COMPOSE_FILE?" || die "stopped at the compose edit"
  backup="$COMPOSE_FILE.bak.$(date +%Y%m%d%H%M%S)"
  cp "$COMPOSE_FILE" "$backup"

  # Reuse the anchor's own indentation so the new entry sits in the same list.
  sed -i "s|^\([ \t]*\)- /etc/localtime:/etc/localtime:ro[ \t]*$|\0\n\1- ./plugins:/plugins|" \
    "$COMPOSE_FILE"

  if (cd "$COMPOSE_DIR" && docker compose config >/dev/null 2>&1); then
    say "  added and validated (backup: $backup)"
    MOUNT_OK=1
    CHANGED+=("$COMPOSE_FILE  (backup: $backup)")
  else
    cp "$backup" "$COMPOSE_FILE"
    say "  edit produced an invalid compose file -- reverted from $backup"
    (cd "$COMPOSE_DIR" && docker compose config 2>&1 | head -5 >&2) || true
    die "could not add the volume automatically; add '- ./plugins:/plugins' by hand"
  fi
fi

# ------------------------------------------------------------------- restart

RESTARTED=0
if [ "$DO_ALL" -eq 1 ] && [ "$MOUNT_OK" -eq 1 ] && [ "$DO_RESTART" -eq 1 ]; then
  step "Restarting Immich"
  confirm "Run 'docker compose up -d' in $COMPOSE_DIR?" || die "stopped before restarting"
  (cd "$COMPOSE_DIR" && docker compose up -d)
  RESTARTED=1

  step "Verifying the plugin loaded"
  # Import happens during bootstrap of the microservices worker.
  found=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    found="$(cd "$COMPOSE_DIR" && docker compose logs immich-server 2>&1 \
      | grep -iE "Imported plugin $PLUGIN_DIR_NAME|Plugin up to date \(name=$PLUGIN_DIR_NAME" | tail -1 || true)"
    [ -n "$found" ] && break
  done
  if [ -n "$found" ]; then
    say "  $found"
  else
    warn "no import line yet. Check with:"
    warn "    cd $COMPOSE_DIR && docker compose logs immich-server | grep -i plugin"
    warn "  and that the container sees the files:"
    warn "    docker compose exec immich-server ls -la /plugins/$PLUGIN_DIR_NAME"
  fi
fi

# -------------------------------------------------------------------- report

step "Summary"
say "  Immich:  $COMPOSE_DIR"
say "  Changed:"
for item in "${CHANGED[@]}"; do say "    $item"; done
[ "$RESTARTED" -eq 1 ] && say "  Restarted: yes" || say "  Restarted: no"

say ""
if [ "$MOUNT_OK" -eq 1 ] && [ "$RESTARTED" -eq 1 ]; then
  say "  Next: create an API key with only 'asset.read', then in Immich go to"
  say "        Workflows -> New (trigger: Asset tagged), add 'Filter by smart"
  say "        search' then 'Add to Album(s)'."
  say "        Or: immich-organizer workflow create --query \"...\" --album \"...\" --apply"
elif [ "$MOUNT_OK" -eq 1 ]; then
  say "  Next: cd $COMPOSE_DIR && docker compose up -d"
else
  say "  Next: add the volume line above, then: cd $COMPOSE_DIR && docker compose up -d"
fi
say "  Rollback: docs/INSTALL-PLUGIN-WSL2.md"

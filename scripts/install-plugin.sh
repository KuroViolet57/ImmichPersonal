#!/usr/bin/env bash
#
# Installs the Smart Album plugin into a Docker Compose Immich deployment.
#
# Run this on the machine where Immich runs -- inside WSL2, not PowerShell.
#
#   ./scripts/install-plugin.sh [/path/to/folder/with/docker-compose.yml]
#
# It copies the plugin into place and adds the two settings to .env (keeping a
# backup). It never edits docker-compose.yml: the one line that belongs there
# is printed for you to add, because indentation mistakes in YAML are easy to
# make and annoying to debug.
set -euo pipefail

PLUGIN_DIR_NAME="immich-smart-album"
RAW_BASE="https://raw.githubusercontent.com/KuroViolet57/ImmichPersonal/claude/immich-album-organization-2sj7f8/plugins/immich-smart-album"
ASSUME_YES=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) COMPOSE_DIR="$1"; shift ;;
  esac
done

# ---------------------------------------------------------------- compose dir

find_compose_dir() {
  for candidate in \
    "$HOME/immich-app" "$HOME/immich" "$HOME/docker/immich" \
    "/opt/immich" "/srv/immich" "$PWD"; do
    if [ -f "$candidate/docker-compose.yml" ] && grep -q 'immich-server' "$candidate/docker-compose.yml" 2>/dev/null; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

step "Locating your Immich deployment"
if [ -z "${COMPOSE_DIR:-}" ]; then
  COMPOSE_DIR="$(find_compose_dir)" || die "could not find docker-compose.yml. Pass the folder: $0 /path/to/immich"
fi
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd)"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
[ -f "$COMPOSE_FILE" ] || die "no docker-compose.yml in $COMPOSE_DIR"
grep -q 'immich-server' "$COMPOSE_FILE" || die "$COMPOSE_FILE does not look like an Immich compose file"
say "  found: $COMPOSE_FILE"

# ------------------------------------------------------------- plugin payload

step "Collecting the plugin files"
SOURCE_DIR="$(cd "$(dirname "$0")/../plugins/$PLUGIN_DIR_NAME" 2>/dev/null && pwd || true)"
TARGET_DIR="$COMPOSE_DIR/plugins/$PLUGIN_DIR_NAME"
mkdir -p "$TARGET_DIR"

if [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/manifest.json" ] && [ -f "$SOURCE_DIR/dist/plugin.wasm" ]; then
  say "  using the copy in this repository"
  cp "$SOURCE_DIR/manifest.json" "$TARGET_DIR/manifest.json"
  cp "$SOURCE_DIR/dist/plugin.wasm" "$TARGET_DIR/plugin.wasm"
else
  say "  downloading from GitHub"
  command -v curl >/dev/null || die "curl is required to download the plugin"
  curl -fsSL -o "$TARGET_DIR/manifest.json" "$RAW_BASE/manifest.json"
  curl -fsSL -o "$TARGET_DIR/plugin.wasm" "$RAW_BASE/dist/plugin.wasm"
fi

# The manifest names the wasm file; if they disagree the server logs a
# confusing read error instead of a validation failure.
WASM_NAME="$(sed -n 's/.*"wasmPath"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TARGET_DIR/manifest.json")"
[ "$WASM_NAME" = "plugin.wasm" ] || die "manifest wasmPath is '$WASM_NAME' but the file was installed as plugin.wasm"
say "  installed into $TARGET_DIR"
say "    manifest.json  $(wc -c < "$TARGET_DIR/manifest.json") bytes"
say "    plugin.wasm    $(wc -c < "$TARGET_DIR/plugin.wasm") bytes"

# ---------------------------------------------------------------------- .env

step "Enabling external plugins in .env"
ENV_FILE="$COMPOSE_DIR/.env"
[ -f "$ENV_FILE" ] || die "no .env in $COMPOSE_DIR (Immich's compose file reads its settings from it)"

needed=""
grep -q '^[[:space:]]*IMMICH_ALLOW_EXTERNAL_PLUGINS=' "$ENV_FILE" \
  || needed="${needed}IMMICH_ALLOW_EXTERNAL_PLUGINS=true"$'\n'
grep -q '^[[:space:]]*IMMICH_PLUGINS_INSTALL_FOLDER=' "$ENV_FILE" \
  || needed="${needed}IMMICH_PLUGINS_INSTALL_FOLDER=/plugins"$'\n'

if [ -z "$needed" ]; then
  say "  both settings already present, leaving .env alone"
else
  say "  these lines need to be added:"
  printf '    %s\n' $needed
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '  Append them to %s? [y/N] ' "$ENV_FILE"
    read -r reply
    case "$reply" in [yY]|[yY][eE][sS]) ;; *) die "stopped; add the lines yourself and re-run" ;; esac
  fi
  backup="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
  cp "$ENV_FILE" "$backup"
  { printf '\n# Added by immich-organizer install-plugin.sh\n'; printf '%s' "$needed"; } >> "$ENV_FILE"
  say "  appended (backup at $backup)"
fi

# ------------------------------------------------------------ compose volume

step "Mounting the plugins folder"
if grep -qE '^[[:space:]]*-[[:space:]]*\./plugins:/plugins' "$COMPOSE_FILE"; then
  say "  the volume is already in docker-compose.yml"
  MOUNT_OK=1
else
  MOUNT_OK=0
  cat <<EOF
  This one line is still missing. Open:

      $COMPOSE_FILE

  and add it under the 'volumes:' list of the immich-server service, so it
  reads like this:

      services:
        immich-server:
          volumes:
            - \${UPLOAD_LOCATION}:/data
            - /etc/localtime:/etc/localtime:ro
            - ./plugins:/plugins          <-- add this line

  Keep the indentation identical to the lines above it.
EOF
fi

step "Next"
if [ "$MOUNT_OK" -eq 1 ]; then
  say "  cd $COMPOSE_DIR && docker compose up -d"
else
  say "  1. add the volume line shown above"
  say "  2. cd $COMPOSE_DIR && docker compose up -d"
fi
say "  then check it loaded:"
say "      docker compose logs immich-server | grep -i plugin"
say "  or, from this repository:"
say "      immich-organizer doctor"

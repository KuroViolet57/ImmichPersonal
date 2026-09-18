#!/usr/bin/env bash
# Builds plugin.wasm from source.
#
# The repository ships a prebuilt dist/plugin.wasm so you do not need this
# unless you have changed the code or would rather not run a binary you did
# not compile. It fetches the two tools extism-js needs if they are missing.
set -euo pipefail

cd "$(dirname "$0")"

EXTISM_JS_VERSION="${EXTISM_JS_VERSION:-v1.6.0}"
BINARYEN_VERSION="${BINARYEN_VERSION:-123}"
TOOLS_DIR="${TOOLS_DIR:-$PWD/.tools}"
export PATH="$TOOLS_DIR:$PATH"

need() { ! command -v "$1" >/dev/null 2>&1; }

if need node; then
  echo "Node.js is required: https://nodejs.org/" >&2
  exit 1
fi

mkdir -p "$TOOLS_DIR"

if need extism-js; then
  echo "Fetching extism-js $EXTISM_JS_VERSION ..."
  arch="$(uname -m)"; os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$os" in
    linux)  asset="extism-js-${arch}-linux-${EXTISM_JS_VERSION}.gz" ;;
    darwin) asset="extism-js-${arch}-macos-${EXTISM_JS_VERSION}.gz" ;;
    *) echo "Unsupported OS: $os. Build inside WSL or Docker." >&2; exit 1 ;;
  esac
  curl -fsSL -o "$TOOLS_DIR/extism-js.gz" \
    "https://github.com/extism/js-pdk/releases/download/${EXTISM_JS_VERSION}/${asset}"
  gunzip -f "$TOOLS_DIR/extism-js.gz"
  chmod +x "$TOOLS_DIR/extism-js"
fi

# extism-js shells out to wasm-merge, which only exists in newer binaryen
# releases -- the version in most distro repositories is too old.
if need wasm-merge; then
  echo "Fetching binaryen $BINARYEN_VERSION ..."
  arch="$(uname -m)"; os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$os" in
    linux)  tarball="binaryen-version_${BINARYEN_VERSION}-${arch}-linux.tar.gz" ;;
    darwin) tarball="binaryen-version_${BINARYEN_VERSION}-${arch}-macos.tar.gz" ;;
    *) echo "Unsupported OS: $os" >&2; exit 1 ;;
  esac
  curl -fsSL -o "$TOOLS_DIR/binaryen.tar.gz" \
    "https://github.com/WebAssembly/binaryen/releases/download/version_${BINARYEN_VERSION}/${tarball}"
  tar xzf "$TOOLS_DIR/binaryen.tar.gz" -C "$TOOLS_DIR"
  cp "$TOOLS_DIR/binaryen-version_${BINARYEN_VERSION}/bin/wasm-merge" \
     "$TOOLS_DIR/binaryen-version_${BINARYEN_VERSION}/bin/wasm-opt" "$TOOLS_DIR/"
  rm -rf "$TOOLS_DIR/binaryen.tar.gz" "$TOOLS_DIR/binaryen-version_${BINARYEN_VERSION}"
fi

echo "Installing npm dependencies ..."
npm install --silent

echo "Building ..."
npm run build

echo "Testing ..."
npm test

echo
echo "Built dist/plugin.wasm ($(du -h dist/plugin.wasm | cut -f1))"
echo "Install it by copying manifest.json and dist/plugin.wasm into your"
echo "Immich plugins folder -- see README.md."

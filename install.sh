#!/bin/sh

set -eu

LATEST_RELEASE_API_URL="${AJ_LATEST_RELEASE_API_URL:-https://api.github.com/repos/PaulGG-Code/aj-connector/releases/latest}"
PYTHON_SPEC="${AJ_PYTHON_SPEC:-3.12}"
UV_INSTALL_URL="${UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"
RUNTIME_CONSTRAINTS_PATH="constraints/aj-runtime.txt"
BUILD_CONSTRAINTS_PATH="constraints/aj-build.txt"
RELEASE_RAW_FILE_URL_BASE="https://raw.githubusercontent.com/PaulGG-Code/aj-connector/refs/tags"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

fetch_latest_release_tag() {
    if have_cmd curl; then
        response="$(curl -fsSL \
            -H "Accept: application/vnd.github+json" \
            -H "User-Agent: aj-cli-installer" \
            "$LATEST_RELEASE_API_URL")" || {
            echo "Could not resolve the latest aj release from GitHub." >&2
            exit 1
        }
    elif have_cmd wget; then
        response="$(wget -qO- \
            --header="Accept: application/vnd.github+json" \
            --header="User-Agent: aj-cli-installer" \
            "$LATEST_RELEASE_API_URL")" || {
            echo "Could not resolve the latest aj release from GitHub." >&2
            exit 1
        }
    else
        echo "curl or wget is required to resolve the latest aj release." >&2
        exit 1
    fi

    tag="$(printf '%s\n' "$response" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [ -z "$tag" ]; then
        echo "GitHub latest-release response did not include tag_name." >&2
        exit 1
    fi
    printf '%s\n' "$tag"
}

release_file_url() {
    printf '%s/%s/%s\n' "$RELEASE_RAW_FILE_URL_BASE" "$1" "$2"
}

is_enabled() {
    case "$1" in
        1|true|TRUE|yes|YES|on|ON|enabled|ENABLED)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

download_file() {
    url="$1"
    output="$2"
    if have_cmd curl; then
        curl -fsSL "$url" -o "$output"
    elif have_cmd wget; then
        wget -qO "$output" "$url"
    else
        echo "curl or wget is required to download release dependency locks." >&2
        exit 1
    fi
}

prepare_constraint() {
    spec="$1"
    name="$2"
    if [ -z "$spec" ]; then
        return
    fi

    case "$spec" in
        http://*|https://*)
            target="$LOCK_TEMP_DIR/$name"
            download_file "$spec" "$target"
            printf '%s\n' "$target"
            ;;
        *)
            if [ ! -f "$spec" ]; then
                echo "Dependency lock file does not exist: $spec" >&2
                exit 1
            fi
            printf '%s\n' "$spec"
            ;;
    esac
}

resolve_release_target() {
    if [ -n "${AJ_PACKAGE_SPEC:-}" ]; then
        PACKAGE_SPEC="$AJ_PACKAGE_SPEC"
        RELEASE_TAG=""
        return
    fi

    RELEASE_TAG="$(fetch_latest_release_tag)"
    PACKAGE_SPEC="aj @ https://github.com/PaulGG-Code/aj-connector/archive/refs/tags/$RELEASE_TAG.zip"
}

resolve_constraints() {
    if [ -n "${AJ_RUNTIME_CONSTRAINTS:-}" ] && [ -n "${AJ_BUILD_CONSTRAINTS:-}" ]; then
        runtime_spec="$AJ_RUNTIME_CONSTRAINTS"
        build_spec="$AJ_BUILD_CONSTRAINTS"
    elif [ -n "$RELEASE_TAG" ]; then
        runtime_spec="$(release_file_url "$RELEASE_TAG" "$RUNTIME_CONSTRAINTS_PATH")"
        build_spec="$(release_file_url "$RELEASE_TAG" "$BUILD_CONSTRAINTS_PATH")"
    elif is_enabled "${AJ_ALLOW_UNPINNED_UPDATE:-}"; then
        runtime_spec=""
        build_spec=""
    else
        cat >&2 <<'EOF'
AJ_PACKAGE_SPEC requires AJ_RUNTIME_CONSTRAINTS and AJ_BUILD_CONSTRAINTS.
Set AJ_ALLOW_UNPINNED_UPDATE=1 only for intentional development installs.
EOF
        exit 1
    fi

    RUNTIME_CONSTRAINTS="$(prepare_constraint "$runtime_spec" "aj-runtime.txt")"
    BUILD_CONSTRAINTS="$(prepare_constraint "$build_spec" "aj-build.txt")"
}

ensure_uv() {
    if have_cmd uv; then
        return
    fi

    if have_cmd curl; then
        curl -LsSf "$UV_INSTALL_URL" | sh
    elif have_cmd wget; then
        wget -qO- "$UV_INSTALL_URL" | sh
    else
        echo "curl or wget is required to install uv." >&2
        exit 1
    fi

    export PATH="$HOME/.local/bin:$PATH"

    if ! have_cmd uv; then
        cat >&2 <<'EOF'
uv was installed but is not on PATH in this shell yet.
Open a new terminal, then rerun this installer.
EOF
        exit 1
    fi
}

print_clipboard_dependency_hint() {
    if [ "$(uname -s 2>/dev/null || true)" != "Linux" ] || have_cmd wl-paste || have_cmd xclip; then
        return
    fi

    cat <<'EOF'

Linux clipboard image paste needs one native helper:
  Wayland: sudo apt install wl-clipboard
  X11:     sudo apt install xclip
EOF
}

main() {
    ensure_uv
    LOCK_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aj-install-locks.XXXXXX")"
    trap 'rm -rf "$LOCK_TEMP_DIR"' EXIT INT TERM

    PACKAGE_SPEC=""
    RELEASE_TAG=""
    RUNTIME_CONSTRAINTS=""
    BUILD_CONSTRAINTS=""
    resolve_release_target
    resolve_constraints

    uv_bin_dir="$(uv tool dir --bin)"
    export PATH="$uv_bin_dir:$PATH"

    uv tool update-shell >/dev/null 2>&1 || true
    set -- uv tool install --force --python "$PYTHON_SPEC" --managed-python --upgrade-package aj
    if [ -n "$RUNTIME_CONSTRAINTS" ]; then
        set -- "$@" --constraints "$RUNTIME_CONSTRAINTS"
    fi
    if [ -n "$BUILD_CONSTRAINTS" ]; then
        set -- "$@" --build-constraints "$BUILD_CONSTRAINTS"
    fi
    if [ -z "$RUNTIME_CONSTRAINTS" ] || [ -z "$BUILD_CONSTRAINTS" ]; then
        echo "Warning: installing aj without dependency locks." >&2
    fi
    "$@" "$PACKAGE_SPEC"

    print_clipboard_dependency_hint

    cat <<EOF

aj is installed.

Run:
  aj

Managed Python:
  $PYTHON_SPEC

If 'aj' is not available in your current shell yet, open a new terminal.
uv installs tool executables in:
  $uv_bin_dir
EOF
}

main "$@"

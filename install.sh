#!/bin/sh

set -eu

LATEST_RELEASE_API_URL="${A0_LATEST_RELEASE_API_URL:-https://api.github.com/repos/agent0ai/a0-connector/releases/latest}"
PYTHON_SPEC="${A0_PYTHON_SPEC:-3.11}"
UV_INSTALL_URL="${UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

fetch_latest_release_tag() {
    if have_cmd curl; then
        response="$(curl -fsSL \
            -H "Accept: application/vnd.github+json" \
            -H "User-Agent: a0-cli-installer" \
            "$LATEST_RELEASE_API_URL")" || {
            echo "Could not resolve the latest a0 release from GitHub." >&2
            exit 1
        }
    elif have_cmd wget; then
        response="$(wget -qO- \
            --header="Accept: application/vnd.github+json" \
            --header="User-Agent: a0-cli-installer" \
            "$LATEST_RELEASE_API_URL")" || {
            echo "Could not resolve the latest a0 release from GitHub." >&2
            exit 1
        }
    else
        echo "curl or wget is required to resolve the latest a0 release." >&2
        exit 1
    fi

    tag="$(printf '%s\n' "$response" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [ -z "$tag" ]; then
        echo "GitHub latest-release response did not include tag_name." >&2
        exit 1
    fi
    printf '%s\n' "$tag"
}

resolve_package_spec() {
    if [ -n "${A0_PACKAGE_SPEC:-}" ]; then
        printf '%s\n' "$A0_PACKAGE_SPEC"
        return
    fi

    tag="$(fetch_latest_release_tag)"
    printf 'a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/%s.zip\n' "$tag"
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

main() {
    ensure_uv
    PACKAGE_SPEC="$(resolve_package_spec)"

    uv_bin_dir="$(uv tool dir --bin)"
    export PATH="$uv_bin_dir:$PATH"

    uv tool update-shell >/dev/null 2>&1 || true
    uv tool install --python "$PYTHON_SPEC" --managed-python --upgrade "$PACKAGE_SPEC"

    cat <<EOF

a0 is installed.

Run:
  a0

Managed Python:
  $PYTHON_SPEC

If 'a0' is not available in your current shell yet, open a new terminal.
uv installs tool executables in:
  $uv_bin_dir
EOF
}

main "$@"

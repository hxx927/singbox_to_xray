#!/bin/sh
set -eu

REPOSITORY=${SINGBOX_TO_XRAY_REPOSITORY:-hxx927/singbox_to_xray}
VERSION=${SINGBOX_TO_XRAY_VERSION:-main}
DESTINATION=${SINGBOX_TO_XRAY_DESTINATION:-/usr/local/bin/singbox-to-xray}
URL="https://raw.githubusercontent.com/${REPOSITORY}/${VERSION}/singbox_to_xray.py"

for command in curl install mktemp python3; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "error: required command not found: $command" >&2
        exit 1
    fi
done

tmp_dir=$(mktemp -d)
tmp_file="$tmp_dir/singbox_to_xray.py"
cleanup() {
    rm -f "$tmp_file"
    rmdir "$tmp_dir" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 \
    "$URL" -o "$tmp_file"
python3 "$tmp_file" --version >/dev/null
install -m 0755 "$tmp_file" "$DESTINATION"

echo "installed singbox-to-xray to $DESTINATION"
echo "run a safe S-UI/Xray preflight: sudo $DESTINATION deploy --strict"

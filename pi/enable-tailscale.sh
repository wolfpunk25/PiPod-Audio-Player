#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
config_file="$project_dir/pipod.env"
tailnet_ip=$(tailscale ip -4 | head -n 1)
case "$tailnet_ip" in
    100.*) ;;
    *) printf 'Pi has no Tailscale IPv4 address yet. Complete tailscale up first.\n' >&2; exit 1 ;;
esac

if [ ! -f "$config_file" ]; then
    printf 'Missing %s; run install.sh first.\n' "$config_file" >&2
    exit 1
fi

# Keep a private copy so the old LAN token setup can be restored if needed.
if [ ! -f "$config_file.before-tailscale" ]; then
    cp "$config_file" "$config_file.before-tailscale"
    chmod 600 "$config_file.before-tailscale"
fi

temp_file=$(mktemp "$config_file.XXXXXX")
trap 'rm -f "$temp_file"' EXIT HUP INT TERM
awk '!/^PIPOD_BIND=/ && !/^PIPOD_TOKEN=/' "$config_file" > "$temp_file"
printf 'PIPOD_BIND=%s\nPIPOD_TOKEN=\n' "$tailnet_ip" >> "$temp_file"
chmod 600 "$temp_file"
mv "$temp_file" "$config_file"
trap - EXIT HUP INT TERM
systemctl --user restart pipod.service
printf 'PiPod is available to your Tailscale devices at http://%s:8080/\n' "$tailnet_ip"

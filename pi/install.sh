#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
user_home=$(getent passwd "$(id -un)" | cut -d: -f6)
units_dir="$user_home/.config/systemd/user"
mkdir -p "$units_dir" "$user_home/Music"

if [ ! -f "$project_dir/pipod.env" ]; then
    old_env="$user_home/macropad-jukebox/jukebox.env"
    if [ -f "$old_env" ]; then
        access_token=$(sed -n 's/^JUKEBOX_TOKEN=//p' "$old_env")
    else
        access_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    fi
    umask 077
    {
        printf 'PIPOD_MUSIC=%s/Music\n' "$user_home"
        printf 'PIPOD_WEB_PORT=8080\n'
        printf 'PIPOD_TOKEN=%s\n' "$access_token"
    } > "$project_dir/pipod.env"
fi

cp "$project_dir/pi/pipod-vlc.service" "$units_dir/pipod-vlc.service"
cp "$project_dir/pi/pipod.service" "$units_dir/pipod.service"
systemctl --user daemon-reload
if systemctl --user cat macropad-jukebox.service >/dev/null 2>&1; then
    systemctl --user disable --now macropad-jukebox.service macropad-vlc.service
fi
systemctl --user enable --now pipod-vlc.service pipod.service
pi_address=$(hostname -I | awk '{print $1}')
printf 'PiPod Audio Player installed. Web page: http://%s:8080/\n' "$pi_address"
printf 'To see the access token: sed -n "s/^PIPOD_TOKEN=//p" %s/pipod.env\n' "$project_dir"

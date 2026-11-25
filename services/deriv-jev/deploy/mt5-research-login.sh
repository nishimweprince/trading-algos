#!/usr/bin/env bash
# Operate only on the isolated research terminal's X display.
set +x
set -euo pipefail
export DISPLAY=:98
server=ICMarketsSC-MT5-3

if ! systemctl is-active --quiet icmarkets-research.service; then
    echo 'The MT5 research terminal service is not running.' >&2
    exit 1
fi
exec 9>/opt/icmarkets-research/export.lock
flock -n 9 || { echo 'Another research login or export is running.' >&2; exit 1; }

terminal=$(xdotool search --onlyvisible --name 'MetaTrader 5|ICMarketsSC-MT5-3' | head -n 1)
find_dialog() {
    xdotool search --onlyvisible --name '^Login$' | head -n 1
}

dialog=$(find_dialog || true)
if [[ -z "$dialog" ]]; then
    xdotool mousemove --window "$terminal" 18 11 click 1
    sleep 0.3
    xdotool mousemove --window "$terminal" 100 244 click 1
    for _ in {1..15}; do
        dialog=$(find_dialog || true)
        [[ -n "$dialog" ]] && break
        sleep 0.2
    done
fi
if [[ -z "$dialog" ]] || ! xdotool getwindowgeometry --shell "$dialog" | grep -qx 'WIDTH=525'; then
    echo 'Could not verify the MT5 Login dialog on research display :98.' >&2
    exit 1
fi
if [[ "${1:-}" == --check ]]; then
    echo 'Research terminal and Login dialog verified.'
    exit 0
fi

read -r -p 'IC Markets account number: ' login </dev/tty
if [[ ! "$login" =~ ^[0-9]+$ ]]; then
    echo 'Enter a numeric account number.' >&2
    exit 1
fi
read -r -s -p 'IC Markets MT5 password: ' password </dev/tty
printf '\n' >/dev/tty
if [[ -z "$password" ]]; then
    echo 'Password is required.' >&2
    exit 1
fi

fill() {
    xdotool mousemove --window "$dialog" "$1" "$2" click 1 key ctrl+a
    printf '%s' "$3" | xdotool type --clearmodifiers --file -
}

# Enter the password last, and only after rechecking that the target is still visible.
[[ "$(find_dialog)" == "$dialog" ]]
fill 184 74 "$login"
fill 292 132 "$server"
fill 184 103 "$password"
unset password
xdotool mousemove --window "$dialog" 217 194 click 1

for _ in {1..30}; do
    title=$(xdotool getwindowname "$terminal")
    if [[ "$title" == "$login - $server "* ]]; then
        echo "Research terminal selected the requested account on $server."
        exit 0
    fi
    sleep 1
done
echo 'MT5 did not confirm the requested account. Check the research terminal journal.' >&2
exit 1

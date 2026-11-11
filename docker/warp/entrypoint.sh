#!/bin/bash

# exit when any command fails
set -e

# Keep both long-running processes in the container's lifetime. The upstream
# entrypoint leaves gost running when warp-svc crashes, which silently falls
# back to the host's direct route and prevents Docker's restart policy firing.
warp_pid=
gost_pid=
monitor_pid=
supervisor_state=$(mktemp -d)
cleanup() {
    trap - EXIT TERM INT
    daemon_pid=
    if [ -r "$supervisor_state/daemon.pid" ]; then
        read -r daemon_pid < "$supervisor_state/daemon.pid" || true
    fi
    for pid in "$gost_pid" "$monitor_pid"; do
        if [ -n "$pid" ]; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    # The container runs as "warp", while warp-svc runs as root under sudo.
    # Record the actual daemon PID instead of assuming sudo's PID is the daemon.
    if [[ "$daemon_pid" =~ ^[0-9]+$ ]]; then
        sudo -n kill -TERM "$daemon_pid" 2>/dev/null || true
    fi
    if [ -n "$warp_pid" ]; then
        # Also cover shutdown before the root child has written its PID file.
        sudo -n kill -TERM "$warp_pid" 2>/dev/null || true
    fi
    # A wedged daemon must not prevent the container's restart policy firing.
    (
        sleep 5 &
        guard_sleep=$!
        trap 'kill -TERM "$guard_sleep" 2>/dev/null || true; wait "$guard_sleep" 2>/dev/null || true; exit 0' TERM INT
        wait "$guard_sleep"
        if [[ "$daemon_pid" =~ ^[0-9]+$ ]]; then
            sudo -n kill -KILL "$daemon_pid" 2>/dev/null || true
        fi
        for pid in "$gost_pid" "$monitor_pid" "$warp_pid"; do
            if [ -n "$pid" ]; then
                sudo -n kill -KILL "$pid" 2>/dev/null || true
            fi
        done
    ) &
    cleanup_guard=$!
    for pid in "$gost_pid" "$monitor_pid" "$warp_pid"; do
        if [ -n "$pid" ]; then
            wait "$pid" 2>/dev/null || true
        fi
    done
    kill -TERM "$cleanup_guard" 2>/dev/null || true
    wait "$cleanup_guard" 2>/dev/null || true
    sudo -n rm -rf -- "$supervisor_state"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# create a tun device if not exist
# allow passing device to ensure compatibility with Podman
if [ ! -e /dev/net/tun ]; then
    sudo mkdir -p /dev/net
    sudo mknod /dev/net/tun c 10 200
    sudo chmod 600 /dev/net/tun
fi

# start dbus
sudo mkdir -p /run/dbus
if [ -f /run/dbus/pid ]; then
    sudo rm /run/dbus/pid
fi
sudo dbus-daemon --config-file=/usr/share/dbus-1/system.conf

# start the daemon
sudo -n bash -c 'printf "%s\n" "$$" > "$1"; exec warp-svc --accept-tos' bash "$supervisor_state/daemon.pid" &
warp_pid=$!

# sleep to wait for the daemon to start, default 2 seconds
sleep "$WARP_SLEEP"

# if /var/lib/cloudflare-warp/reg.json not exists, setup new warp client
if [ ! -f /var/lib/cloudflare-warp/reg.json ]; then
    # if /var/lib/cloudflare-warp/mdm.xml not exists or REGISTER_WHEN_MDM_EXISTS not empty, register the warp client
    if [ ! -f /var/lib/cloudflare-warp/mdm.xml ] || [ -n "$REGISTER_WHEN_MDM_EXISTS" ]; then
        warp-cli registration new && echo "Warp client registered!"
        # if a license key is provided, register the license
        if [ -n "$WARP_LICENSE_KEY" ]; then
            echo "License key found, registering license..."
            warp-cli registration license "$WARP_LICENSE_KEY" && echo "Warp license registered!"
        fi
    fi
else
    echo "Warp client already registered, skip registration"
fi
# An existing registration does not imply the tunnel is connected after a
# daemon crash or a persisted disconnect. Reconnect it without re-registering.
warp-cli --accept-tos connect

# disable qlog if DEBUG_ENABLE_QLOG is empty
if [ -z "$DEBUG_ENABLE_QLOG" ]; then
    warp-cli --accept-tos debug qlog disable
else
    warp-cli --accept-tos debug qlog enable
fi

# if WARP_ENABLE_NAT is provided, enable NAT and forwarding
if [ -n "$WARP_ENABLE_NAT" ]; then
    # switch to warp mode
    echo "[NAT] Switching to warp mode..."
    warp-cli --accept-tos mode warp
    warp-cli --accept-tos connect

    # wait another seconds for the daemon to reconfigure
    sleep "$WARP_SLEEP"

    # enable NAT
    echo "[NAT] Enabling NAT..."
    sudo nft add table ip nat
    sudo nft add chain ip nat WARP_NAT { type nat hook postrouting priority 100 \; }
    sudo nft add rule ip nat WARP_NAT oifname "CloudflareWARP" masquerade
    sudo nft add table ip mangle
    sudo nft add chain ip mangle forward { type filter hook forward priority mangle \; }
    sudo nft add rule ip mangle forward tcp flags syn tcp option maxseg size set rt mtu

    sudo nft add table ip6 nat
    sudo nft add chain ip6 nat WARP_NAT { type nat hook postrouting priority 100 \; }
    sudo nft add rule ip6 nat WARP_NAT oifname "CloudflareWARP" masquerade
    sudo nft add table ip6 mangle
    sudo nft add chain ip6 mangle forward { type filter hook forward priority mangle \; }
    sudo nft add rule ip6 mangle forward tcp flags syn tcp option maxseg size set rt mtu
fi

# Do not accept proxy traffic while the tunnel is still starting. This guards
# startup only; it is not a replacement for a network-level kill switch.
ready_deadline=$((SECONDS + 60))
while true; do
    if ! kill -0 "$warp_pid" 2>/dev/null; then
        echo "WARP daemon exited before the tunnel was ready" >&2
        exit 1
    fi
    if status=$(timeout 5 warp-cli --accept-tos status 2>/dev/null) &&
        grep -qx 'Status update: Connected' <<< "$status"; then
        break
    fi
    if [ "$SECONDS" -ge "$ready_deadline" ]; then
        echo "WARP tunnel did not become ready within 60 seconds" >&2
        exit 1
    fi
    sleep "$WARP_SLEEP"
done

# A live daemon can lose its tunnel too. Stop accepting proxy traffic if its
# status becomes disconnected/unreadable; let Docker restart the whole chain.
# This bounded polling is not a network-level kill switch, nor proof that a
# particular upstream (such as ChatGPT) accepts the current WARP exit address.
monitor_tunnel() {
    while sleep "$WARP_SLEEP"; do
        if ! status=$(timeout 5 warp-cli --accept-tos status 2>/dev/null) ||
            ! grep -qx 'Status update: Connected' <<< "$status"; then
            echo "WARP tunnel is no longer connected; stopping proxy" >&2
            return 1
        fi
    done
}

# A crash (or even an unexpected clean exit) of either process terminates the
# container, allowing restart: unless-stopped to restore the complete chain.
gost $GOST_ARGS &
gost_pid=$!
monitor_tunnel &
monitor_pid=$!
if wait -n "$warp_pid" "$gost_pid" "$monitor_pid"; then
    exit_code=1
else
    exit_code=$?
fi
echo "WARP daemon or proxy exited; restarting the complete proxy chain" >&2
exit "$exit_code"

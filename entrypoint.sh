#!/bin/sh
set -e

# Sensible locale / timezone defaults so Chromium does not look like a bare
# UTC server (Turnstile reads these).  Overridable from the environment.
export TZ="${TZ:-Europe/Berlin}"
export LANG="${LANG:-de_DE.UTF-8}"
export LANGUAGE="${LANGUAGE:-de_DE:de}"

# Heuristic VPN detection: when the container shares a VPN container's network
# namespace (e.g. Gluetun via network_mode: service/container:gluetun), a
# WireGuard/OpenVPN interface is present in this namespace (wg0/tun0 by default;
# the WireGuard name is configurable via Gluetun's WIREGUARD_INTERFACE). Match by
# prefix so custom names like wg1/tun1 are covered too. Purely informational — it
# fixes nothing, but surfaces the two usual misconfigurations in the log so a
# "WebUI unreachable" is easy to diagnose. Silent for non-VPN setups.
vpn_iface=""
for iface_path in /sys/class/net/wg* /sys/class/net/tun*; do
    [ -e "$iface_path" ] || continue
    vpn_iface="${iface_path##*/}"
    break
done
if [ -n "$vpn_iface" ]; then
    echo "[MediaForge] VPN network namespace detected (interface: ${vpn_iface})."
    echo "[MediaForge]   -> Publish the WebUI port on the VPN container, not on mediaforge."
    echo "[MediaForge]   -> Set FIREWALL_OUTBOUND_SUBNETS to your LAN subnet if the WebUI is unreachable."
fi

# Start a session D-Bus so Chromium finds the services it expects; missing
# D-Bus is a small "automated container" signal.
if command -v dbus-launch >/dev/null 2>&1; then
    echo "[MediaForge] Starting D-Bus..."
    eval "$(dbus-launch --sh-syntax)" 2>/dev/null || true
fi

echo "[MediaForge] Cleaning up old Xvfb locks..."
rm -f /tmp/.X99-lock
rm -rf /tmp/.X11-unix/X99

echo "[MediaForge] Starting virtual display (Xvfb)..."
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &

echo "[MediaForge] Waiting for display to be ready..."
i=0
until xdpyinfo -display :99 >/dev/null 2>&1; do
    sleep 0.2
    i=$((i + 1))
    if [ $((i % 15)) -eq 0 ]; then
        echo "[MediaForge] Still waiting for Xvfb... (${i} attempts)"
    fi
done
echo "[MediaForge] Display ready."

echo "[MediaForge] Starting MediaForge..."
exec mediaforge -wP 8080 -wN -wH 0.0.0.0

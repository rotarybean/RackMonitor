#!/usr/bin/env bash
# Install node_exporter on gameserver (GAMESERVER_IP) with the systemd collector.
# Run as root:  sudo ./install_node_exporter.sh
set -euo pipefail

VERSION="1.11.1"                 # latest as of 2026-04-07
ARCH="linux-amd64"               # change if gameserver isn't x86_64
LEPOTATO_IP="LEPOTATO_IP"    # <-- set this; used for the firewall rule
TARBALL="node_exporter-${VERSION}.${ARCH}.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"

# Resolve our own directory to an ABSOLUTE path now - we cd to /tmp below, which
# would otherwise break any relative reference to node_exporter.service.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${SCRIPT_DIR}/node_exporter.service" ]]; then
    echo "ERROR: node_exporter.service not found next to this script (${SCRIPT_DIR})" >&2
    exit 1
fi

echo "==> downloading node_exporter ${VERSION}"
cd /tmp
curl -fsSLO "${URL}"
curl -fsSLO "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/sha256sums.txt"
grep " ${TARBALL}\$" sha256sums.txt | sha256sum -c -

echo "==> installing binary"
tar xzf "${TARBALL}"
install -m 0755 "node_exporter-${VERSION}.${ARCH}/node_exporter" /usr/local/bin/node_exporter

echo "==> creating service account"
id -u node_exporter &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter

echo "==> installing unit"
install -m 0644 "${SCRIPT_DIR}/node_exporter.service" /etc/systemd/system/node_exporter.service
systemctl daemon-reload
systemctl enable --now node_exporter
sleep 2
systemctl --no-pager --lines=5 status node_exporter || true

echo "==> firewall (only the Le Potato may scrape :9100)"
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow from "${LEPOTATO_IP}" to any port 9100 proto tcp comment 'rackmonitor'
fi

echo
echo "==> verifying the metrics we actually need"
# Scrape ONCE into a variable. Do not pipe curl into `grep -q`: grep exits on the
# first match, curl takes a SIGPIPE, and `set -o pipefail` turns that into a false
# "missing" for any metric that appears early in the output.
METRICS="$(curl -s http://GAMESERVER_IP:9100/metrics)"

for M in node_time_seconds node_boot_time_seconds node_memory_MemTotal_bytes \
         node_memory_MemAvailable_bytes node_systemd_unit_state \
         node_systemd_unit_start_time_seconds; do
    if grep -q "^${M}" <<<"${METRICS}"; then
        echo "  [ OK ] ${M}"
    else
        echo "  [FAIL] ${M}   <-- missing!"
    fi
done

echo
echo "==> collector health (authoritative - 1 = collector ran clean)"
grep -E '^node_scrape_collector_success\{collector="(meminfo|stat|systemd|time)"\}' <<<"${METRICS}" || true

echo
echo "Systemd unit states as seen by the exporter:"
grep -E '^node_systemd_unit_(state|start_time)' <<<"${METRICS}" || true

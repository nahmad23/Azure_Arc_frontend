#!/usr/bin/env bash
# Check the frontend VIP and each node, and show which node holds the VIP.
# Usage: scripts/check-ha.sh <VIP> <node1-ip> <node2-ip>
set -euo pipefail

vip=${1:?usage: $0 <VIP> <node1-ip> <node2-ip>}
shift
nodes=("$@")

check() {
  local label=$1 addr=$2 served_by
  if served_by=$(curl -fsS --max-time 3 -D - -o /dev/null "http://${addr}/healthz" \
      | awk -F': ' 'tolower($1)=="x-served-by" {print $2}' | tr -d '\r'); then
    printf '%-6s %-15s UP   served-by=%s\n' "$label" "$addr" "${served_by:-?}"
  else
    printf '%-6s %-15s DOWN\n' "$label" "$addr"
    return 1
  fi
}

rc=0
check VIP "$vip" || rc=1
for n in "${nodes[@]}"; do
  check node "$n" || rc=1
done
exit $rc

#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  printf 'usage: %s <safe-node-id>\n' "$0" >&2
  exit 2
fi

node_id="$1"
cert_dir="knowledge-hub/deploy/emqx/certs"
if [[ ! -f "$cert_dir/ca.crt" || ! -f "$cert_dir/ca.key" ]]; then
  printf 'generate the MQTT CA first with scripts/generate-mqtt-certs.sh\n' >&2
  exit 1
fi

umask 077
openssl genrsa -out "$cert_dir/$node_id.key" 3072
openssl req -new -key "$cert_dir/$node_id.key" -subj "/CN=$node_id" -out "$cert_dir/$node_id.csr"
openssl x509 -req -sha256 -days 825 -in "$cert_dir/$node_id.csr" \
  -CA "$cert_dir/ca.crt" -CAkey "$cert_dir/ca.key" -CAcreateserial \
  -extfile <(printf 'extendedKeyUsage=clientAuth\n') -out "$cert_dir/$node_id.crt"
rm -f "$cert_dir/$node_id.csr" "$cert_dir/ca.srl"
chmod 600 "$cert_dir/$node_id.key"
printf 'created mTLS certificate for node %s\n' "$node_id"

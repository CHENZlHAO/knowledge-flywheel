#!/usr/bin/env bash
set -euo pipefail

cert_dir="${1:-knowledge-hub/deploy/emqx/certs}"
server_name="${MQTT_SERVER_NAME:-emqx}"

mkdir -p "$cert_dir"
umask 077

openssl genrsa -out "$cert_dir/ca.key" 4096
openssl req -x509 -new -sha256 -days 3650 -key "$cert_dir/ca.key" \
  -subj "/CN=Knowledge Flywheel MQTT CA" -out "$cert_dir/ca.crt"

openssl genrsa -out "$cert_dir/server.key" 3072
openssl req -new -key "$cert_dir/server.key" -subj "/CN=$server_name" -out "$cert_dir/server.csr"
openssl x509 -req -sha256 -days 825 -in "$cert_dir/server.csr" \
  -CA "$cert_dir/ca.crt" -CAkey "$cert_dir/ca.key" -CAcreateserial \
  -extfile <(printf 'subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' "$server_name") \
  -out "$cert_dir/server.crt"

openssl genrsa -out "$cert_dir/bridge.key" 3072
openssl req -new -key "$cert_dir/bridge.key" -subj "/CN=knowledge-hub-status-bridge" -out "$cert_dir/bridge.csr"
openssl x509 -req -sha256 -days 825 -in "$cert_dir/bridge.csr" \
  -CA "$cert_dir/ca.crt" -CAkey "$cert_dir/ca.key" -CAcreateserial \
  -extfile <(printf 'extendedKeyUsage=clientAuth\n') -out "$cert_dir/bridge.crt"

rm -f "$cert_dir/server.csr" "$cert_dir/bridge.csr" "$cert_dir/ca.srl"
chmod 600 "$cert_dir"/*.key
printf 'MQTT certificates generated in %s\n' "$cert_dir"

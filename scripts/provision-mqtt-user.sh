#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ || ${#2} -lt 12 ]]; then
  printf 'usage: %s <node-id> <strong-password (12+ chars)>\n' "$0" >&2
  exit 2
fi

node_id="$1"
password="$2"
api_url="${EMQX_API_URL:-http://127.0.0.1:18083}"
admin_user="${EMQX_ADMIN_USERNAME:-admin}"
admin_password="${EMQX_ADMIN_PASSWORD:-}"
if [[ -z "$admin_password" ]]; then
  printf 'EMQX_ADMIN_PASSWORD is required (EMQX 5.8 REST API)\n' >&2
  exit 2
fi
command -v curl >/dev/null || { printf 'curl is required\n' >&2; exit 2; }
command -v jq >/dev/null || { printf 'jq is required\n' >&2; exit 2; }

login_body=$(jq -n --arg username "$admin_user" --arg password "$admin_password" '{username:$username,password:$password}')
login_response=$(curl --fail-with-body --silent --show-error --request POST \
  --url "${api_url%/}/api/v5/login" --header 'Content-Type: application/json' --data "$login_body")
token=$(printf '%s' "$login_response" | jq -er '.token')
user_body=$(jq -n --arg user_id "$node_id" --arg password "$password" '{user_id:$user_id,password:$password,is_superuser:false}')
curl --fail-with-body --silent --show-error --request POST \
  --url "${api_url%/}/api/v5/authentication/password_based:built_in_database/users" \
  --header "Authorization: Bearer $token" --header 'Content-Type: application/json' --data "$user_body"
printf '\nMQTT user provisioned: %s\n' "$node_id"

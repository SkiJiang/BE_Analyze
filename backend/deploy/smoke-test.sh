#!/usr/bin/env bash
set -euo pipefail

curl --fail --silent --show-error http://127.0.0.1:8000/health/live
curl --fail --silent --show-error https://"$PUBLIC_HOST"/health/live
test "$(curl --silent --output /dev/null --write-out '%{http_code}' https://"$PUBLIC_HOST"/api/dashboard)" = "401"
ss -lnt | grep -q '127.0.0.1:8000'
! ss -lnt | grep -q '0.0.0.0:8000'

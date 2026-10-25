#!/usr/bin/env bash
# Generate a self-signed cert the fake-cloud probe will present to the AC.
# If the AC's firmware does not validate the server certificate, this is enough.
set -euo pipefail
cd "$(dirname "$0")"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout server.key -out server.crt -days 3650 \
  -subj "/C=BR/O=FakeCloud/CN=*.hijuconn.com" \
  -addext "subjectAltName=DNS:*.hijuconn.com,DNS:hijuconn.com,DNS:*.connectlife.io,DNS:connectlife.io"

echo "Wrote $(pwd)/server.crt and server.key"

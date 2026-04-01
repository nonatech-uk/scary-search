#!/bin/bash
set -euo pipefail

: "${JOPLIN_SERVER_URL:?JOPLIN_SERVER_URL is required}"
: "${JOPLIN_SERVER_EMAIL:?JOPLIN_SERVER_EMAIL is required}"
: "${JOPLIN_SERVER_PASSWORD:?JOPLIN_SERVER_PASSWORD is required}"
: "${JOPLIN_API_TOKEN:?JOPLIN_API_TOKEN is required}"
JOPLIN_SYNC_INTERVAL="${JOPLIN_SYNC_INTERVAL:-300}"

echo "Configuring Joplin CLI..."
joplin config sync.target 9
joplin config sync.9.path "$JOPLIN_SERVER_URL"
joplin config sync.9.username "$JOPLIN_SERVER_EMAIL"
joplin config sync.9.password "$JOPLIN_SERVER_PASSWORD"
joplin config api.port 41184
joplin config api.token "$JOPLIN_API_TOKEN"

echo "Running initial sync..."
joplin sync || echo "WARNING: Initial sync failed, will retry in background"

# Background sync loop
(
    while true; do
        sleep "$JOPLIN_SYNC_INTERVAL"
        joplin sync 2>&1 | tail -1 || true
    done
) &

echo "Starting sync trigger on port 41186..."
node -e "
const http = require('http');
const { execFile } = require('child_process');
http.createServer((req, res) => {
  if (req.url === '/sync') {
    execFile('joplin', ['sync'], { timeout: 60000 }, (err, stdout, stderr) => {
      const out = (stdout || '').trim();
      const msg = err ? 'Sync failed: ' + (stderr || err.message) : 'Sync complete: ' + out;
      res.writeHead(err ? 500 : 200, { 'Content-Type': 'text/plain' });
      res.end(msg);
    });
  } else {
    res.writeHead(404);
    res.end('Not found');
  }
}).listen(41186, '0.0.0.0');
" &

echo "Starting Joplin API server on port 41184..."
# joplin server start binds to 127.0.0.1, so use socat to expose on 0.0.0.0
socat TCP-LISTEN:41184,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:41185 &

joplin config api.port 41185
exec joplin server start

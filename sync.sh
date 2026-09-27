#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

OUTPUT_DIR="${OUTPUT_DIR:-lists}"
MAX_MOVIES="${MAX_MOVIES:-25}"

LISTS=(
  "https://www.allocine.fr/film/aucinema/"
  "https://www.allocine.fr/film/attendus/"
)

# JSON, TMDb and MDBList lists are the primary channels: resolved from Wikidata,
# no Trakt account involved. Publish them first so a broken Trakt never blocks them.
TARGETS=(--to json)
if [ -n "$TMDB_ACCESS_TOKEN" ]; then
  TARGETS+=(--to tmdb)
fi
if [ -n "$MDBLIST_API_KEY" ]; then
  TARGETS+=(--to mdblist)
fi

# One list failing its guards must not stop the others from updating.
failed=0
for url in "${LISTS[@]}"; do
  echo "=== Publishing $url (${TARGETS[*]}) ==="
  if ! python main.py publish "$url" --max-movies "$MAX_MOVIES" --output-dir "$OUTPUT_DIR" "${TARGETS[@]}"; then
    echo "!!! Publishing failed for $url"
    failed=1
  fi
done

# Trakt is best-effort: it needs credentials and a live OAuth app, and a failure
# there must not fail the run.
if [ -z "$TRAKT_CLIENT_ID" ] || [ -z "$TRAKT_CLIENT_SECRET" ]; then
  echo "=== Trakt credentials not set, skipping Trakt sync ==="
  exit "$failed"
fi

for url in "${LISTS[@]}"; do
  echo "=== Syncing $url to Trakt ==="
  if ! python main.py sync-to-trakt "$url" --max-movies "$MAX_MOVIES"; then
    echo "!!! Trakt sync failed for $url - other lists are still published"
  fi
  echo "Waiting 2 minutes before next sync..."
  sleep 120
done

exit "$failed"

#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

python main.py sync-to-trakt https://www.allocine.fr/film/aucinema/ --max-movies 25

echo "Waiting 2 minutes before next sync..."
sleep 120

python main.py sync-to-trakt https://www.allocine.fr/film/attendus/ --max-movies 25

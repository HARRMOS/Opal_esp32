#!/usr/bin/env bash
# Met le VPS à jour depuis GitHub (branche main) puis reconstruit le pont Opal Box.
#   ssh ubuntu@51.210.177.60 /home/ubuntu/Opal_esp32/deploy/update.sh
# Les secrets (opal-bridge.env, models/) sont ignorés par Git : ils ne bougent pas.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch --quiet origin
git merge --ff-only origin/main   # refuse plutôt que d'écraser des modifs faites sur le VPS
git log --oneline -1
cd deploy
docker compose -p opal-box up -d --build

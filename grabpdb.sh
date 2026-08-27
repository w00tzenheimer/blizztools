#!/usr/bin/env bash
set -euo pipefail
python -m blizztools.main  grab -p '.*.pdb$' -p '.*.dSYM' -p '.*.bak' -p '.*_loader.dll' -p 'Wow.*exe' -p 'World\ of\ Warcraft$'  -d ../wow
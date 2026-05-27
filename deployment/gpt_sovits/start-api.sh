#!/usr/bin/env bash
set -euo pipefail

g2pw_source=/workspace/models/G2PWModel
g2pw_target=/workspace/GPT-SoVITS/GPT_SoVITS/text/G2PWModel

# Some upstream images bundle G2PWModel outside the path expected by the app.
if [[ ! -e "$g2pw_target" ]]; then
  if [[ ! -d "$g2pw_source" ]]; then
    echo "GPT-SoVITS image does not contain G2PWModel at $g2pw_source." >&2
    exit 1
  fi
  ln -sfn "$g2pw_source" "$g2pw_target"
fi

cp /run/readio/tts_infer.yaml /tmp/readio_tts_infer.yaml

exec python api_v2.py -a 0.0.0.0 -p 9880 -c /tmp/readio_tts_infer.yaml

#!/usr/bin/env bash
set -euo pipefail

# The image stores optional frontend assets separately from the source tree.
ln -sfn /workspace/models/G2PWModel /workspace/GPT-SoVITS/GPT_SoVITS/text/G2PWModel
cp /run/readio/tts_infer.yaml /tmp/readio_tts_infer.yaml

exec python api_v2.py -a 0.0.0.0 -p 9880 -c /tmp/readio_tts_infer.yaml

#!/usr/bin/env bash
# Regenerate the modbus-hub gRPC client stubs in src/nd45_dtsu666/hubpb/.
#
# The contract's source of truth is korneliuszm/modbus-hub
# (proto/modbushub/v1/hub.proto). A copy is vendored here so this repo can
# build without checking out the hub, and so a contract change shows up as a
# reviewable diff rather than as a silent runtime mismatch.
#
# The generated files are COMMITTED on purpose: neither CI nor the target
# device should need grpcio-tools installed.
#
# Usage:
#   pip install grpcio-tools
#   ./scripts/gen_hub_stubs.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROTO_DIR="proto/modbushub/v1"
OUT_DIR="src/nd45_dtsu666/hubpb"

mkdir -p "$OUT_DIR"
python -m grpc_tools.protoc \
  --proto_path="$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR/hub.proto"

# protoc emits a top-level `import hub_pb2`, which only resolves if the output
# directory happens to be on sys.path. Inside a package it must be relative.
# This rewrite is the standard workaround and has to be reapplied on every
# regeneration.
python - "$OUT_DIR/hub_pb2_grpc.py" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
patched = re.sub(r'^import hub_pb2 as hub__pb2$',
                 'from . import hub_pb2 as hub__pb2',
                 text, flags=re.MULTILINE)
if patched == text and 'from . import hub_pb2' not in text:
    sys.exit("could not patch the hub_pb2 import in %s" % path)
path.write_text(patched)
PY

echo "regenerated $OUT_DIR (remember to commit it)"

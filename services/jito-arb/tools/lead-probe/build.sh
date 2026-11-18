#!/usr/bin/env bash
# Build the lead-time probe on the VM (needs protoc, grpc_cpp_plugin, grpc++, protobuf, OpenSSL dev packages).
#   bash services/jito-arb/tools/lead-probe/build.sh /tmp/lead-probe
set -euo pipefail
src=$(cd "$(dirname "$0")/../.." && pwd)
out=${1:?usage: build.sh /tmp/lead-probe}
mkdir -p "$out"
out=$(cd "$out" && pwd)
protoc -I "$src/proto" --cpp_out="$out" --grpc_out="$out" \
  --plugin=protoc-gen-grpc="$(command -v grpc_cpp_plugin)" \
  "$src/proto/geyser.proto" "$src/proto/solana-storage.proto"
# Amazon Linux grpc++.pc includes /usr/usr/include; normalize that packaging typo.
read -ra flags <<< "$(pkg-config --cflags --libs grpc++ protobuf absl_cord openssl | sed 's@/usr/usr/include@/usr/include@g')"
g++ -std=c++17 -O2 -pthread -I "$out" "$src/tools/lead-probe/probe.cpp" \
  "$out/geyser.pb.cc" "$out/geyser.grpc.pb.cc" \
  "$out/solana-storage.pb.cc" "$out/solana-storage.grpc.pb.cc" \
  "${flags[@]}" -std=c++17 -o "$out/probe"

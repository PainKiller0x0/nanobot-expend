#!/usr/bin/env bash
set -euo pipefail

root="${1:-$(git rev-parse --show-toplevel)}"
if [[ ! -d "$root/sources" ]]; then
  echo "sources/ directory not found: $root" >&2
  exit 1
fi

mapfile -t crates < <(find "$root/sources" -mindepth 2 -maxdepth 2 -name Cargo.toml -printf '%h\n' | sort)
if [[ ${#crates[@]} -eq 0 ]]; then
  echo "No Rust crates found under sources/."
  exit 0
fi

for crate in "${crates[@]}"; do
  rel="${crate#$root/}"
  echo "::group::cargo test --locked $rel"
  if [[ ! -f "$crate/Cargo.lock" ]]; then
    echo "::error file=$rel/Cargo.lock::Missing lockfile. Run cargo generate-lockfile before pushing."
    exit 1
  fi
  (cd "$crate" && cargo test --locked)
  echo "::endgroup::"
done
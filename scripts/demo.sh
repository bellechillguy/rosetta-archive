#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
work_dir="$project_dir/demo-work"
export PYTHONPATH="$project_dir/src"

rm -rf "$work_dir"
mkdir -p "$work_dir/input/nested/empty"
printf 'Rosetta Archive demo\n%.0s' {1..100} > "$work_dir/input/repeated.txt"
python3 -c 'from pathlib import Path; Path("'"$work_dir"'/input/nested/binary.bin").write_bytes(bytes(range(256)) + b"\x00\xff")'

run_rta() {
  python3 -m rosetta_archive "$@"
}

echo '[1/9] create'
run_rta create "$work_dir/demo.rta" "$work_dir/input"

echo '[2/9] list'
run_rta list "$work_dir/demo.rta"

echo '[3/9] info'
run_rta info "$work_dir/demo.rta"

echo '[4/9] inspect'
run_rta inspect "$work_dir/demo.rta"

echo '[5/9] verify'
run_rta verify "$work_dir/demo.rta"

echo '[6/9] extract and compare'
run_rta extract "$work_dir/demo.rta" "$work_dir/output"
diff -r "$work_dir/input" "$work_dir/output/input"

echo '[7/9] determinism'
run_rta create "$work_dir/deterministic-a.rta" "$work_dir/input"
run_rta create "$work_dir/deterministic-b.rta" "$work_dir/input"
cmp "$work_dir/deterministic-a.rta" "$work_dir/deterministic-b.rta"
echo 'deterministic archives are byte-identical'

echo '[8/9] corrupt one payload and show rejection'
cp "$work_dir/demo.rta" "$work_dir/damaged.rta"
python3 -c 'from pathlib import Path; from rosetta_archive import ArchiveReader; p=Path("'"$work_dir"'/damaged.rta"); r=ArchiveReader(p); e=next(x for x in r.entries if x.stored_size); b=bytearray(p.read_bytes()); b[e.data_offset] ^= 1; p.write_bytes(b)'
if run_rta verify "$work_dir/damaged.rta"; then
  echo 'expected damaged archive rejection' >&2
  exit 1
else
  echo 'damaged archive rejected as expected'
fi

echo '[9/9] recover valid entries and run tests'
run_rta recover "$work_dir/damaged.rta" "$work_dir/recovered.rta"
run_rta verify "$work_dir/recovered.rta"
run_rta list "$work_dir/recovered.rta"
python3 -m unittest discover -s "$project_dir/tests" -v

echo "demo complete: $work_dir"


#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== gofmt =="
out="$(cd "${ROOT}/mini-kueue" && gofmt -l .)"
if [ -n "${out}" ]; then
  echo "unformatted files:"
  echo "${out}"
  exit 1
fi
echo "ok"

echo "== go vet =="
(cd "${ROOT}/mini-kueue" && go vet ./...)

echo "== go build =="
(cd "${ROOT}/mini-kueue" && go build ./...)

echo "== go test =="
(cd "${ROOT}/mini-kueue" && go test ./... -count=1)

echo "== scenarios =="
cd "${ROOT}/mini-kueue"
scenario_count="$(go run ./cmd/minikueue -list | wc -l | tr -d ' ')"
if [ "${scenario_count}" != "28" ]; then
  echo "expected 28 CLI scenarios, got ${scenario_count}"
  exit 1
fi
for s in $(go run ./cmd/minikueue -list | awk '{print $1}'); do
  go run ./cmd/minikueue -scenario "$s" -ticks 5 >/dev/null
  echo "ok ${s}"
done

echo "== cli =="
go run ./cmd/minikueue -scenario basic -ticks 2 -query basic >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get workloads >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get clusterqueues >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get localqueues >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get cohorts >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get flavors >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -get topologies >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -describe default/job-a >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -stop default/job-b >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -resume default/job-b >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -explain default/job-c >/dev/null
go run ./cmd/minikueue -scenario basic -ticks 1 -what-if "basic/default/cpu=3000" >/dev/null
go run ./cmd/minikueue -scenario cohort-borrow -ticks 1 -explain team-b/b-normal >/dev/null
go run ./cmd/minikueue -scenario tas -ticks 1 -explain default/job-c >/dev/null
go run ./cmd/minikueue -scenario reclaim -ticks 0 -explain team-a/a-owner >/dev/null
echo "ok"

echo "== docs =="
cd "${ROOT}"
python scripts/verify_docs.py

echo "== config lint =="
for f in examples/config/*.yaml; do
  python scripts/check_kueue_config.py --strict "$f" >/dev/null
done
if python scripts/check_kueue_config.py scripts/testdata/invalid-config.yaml >/dev/null 2>&1; then
  echo "config linter accepted an invalid fixture"
  exit 1
fi
echo "ok"

echo "== capacity model =="
python scripts/capacity_model.py \
  --nodes 12 --gpus-per-node 8 \
  --arrival-rate 8 --mean-duration 4 \
  --job-size-mix "1:0.45,2:0.2,4:0.2,8:0.15" \
  --policy best-effort --hours 2160 --json >/dev/null
python scripts/capacity_model.py \
  --nodes 12 --gpus-per-node 8 \
  --arrival-rate 0.5 --mean-duration 6 --job-size 8 --hours 2160 >/dev/null
echo "ok"

echo "all checks passed"

.PHONY: all test vet fmt check cover bench stress run list clean

all: check

fmt:
	gofmt -w .

vet:
	go vet ./...

test:
	go test ./... -count=1

cover:
	go test ./... -cover -count=1

bench:
	go test ./internal/scheduler -bench . -benchmem -run '^$$'

stress:
	go test ./... -count=10
	go test ./... -count=1 -shuffle=on

check: fmt vet test

list:
	go run ./cmd/minikueue -list

run:
	go run ./cmd/minikueue -scenario basic -ticks 4

clean:
	go clean ./...

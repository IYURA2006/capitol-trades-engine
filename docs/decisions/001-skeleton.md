# 001: Skeleton and migrations

**Decision.** One Go module for the coordinator, one Python package for the SDK, one `.proto` file shared by both. Generated code is committed. Migrations are plain `.sql` files embedded in the coordinator binary and applied at start, inside one transaction that first takes a Postgres advisory lock.

**Alternative.** A migration tool (golang-migrate, Flyway) or a separate "migrate" container that runs before the coordinators.

**Why.** Two coordinators start at the same moment, so both try to create the tables. The advisory lock makes the second one wait; it then sees the migrations are done and skips them. That is about 40 lines of code, needs no extra tool, and is easy to explain. Committing generated code means Docker builds and CI do not need `protoc`.

**Notes.** Compose maps Postgres to host port 5433 so it does not clash with a local Postgres. The Go version in the Dockerfile must match the `go` line in `go.mod`.

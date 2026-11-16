// Command coordinator serves the Engine gRPC API.
//
// It keeps no state of its own (design rule 1), so any number of copies
// can run at the same time and any of them can be killed.
package main

import (
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"

	"capitoltrades/coordinator/internal/engine"
	"capitoltrades/coordinator/internal/pb"
	"capitoltrades/coordinator/internal/server"
	"capitoltrades/coordinator/internal/store"
	"capitoltrades/coordinator/migrations"
)

// maxMessageBytes lets a 10,000-task sub-DAG fit in one Complete call.
const maxMessageBytes = 16 << 20

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if err := run(); err != nil {
		log.Fatalf("coordinator: %v", err)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	dbURL := env("DATABASE_URL", "postgres://engine:engine@localhost:5433/engine?sslmode=disable")
	grpcAddr := env("GRPC_ADDR", ":50051")
	httpAddr := env("HTTP_ADDR", ":8080")

	pool, err := connect(ctx, dbURL)
	if err != nil {
		return err
	}
	defer pool.Close()

	if err := store.Migrate(ctx, pool, migrations.FS); err != nil {
		return err
	}
	log.Printf("migrations applied")

	cfg := engine.DefaultConfig()
	cfg.Lease = envDuration("LEASE", cfg.Lease)
	cfg.ReaperInterval = envDuration("REAPER_INTERVAL", cfg.ReaperInterval)
	cfg.BackoffBase = envDuration("BACKOFF_BASE", cfg.BackoffBase)
	cfg.BackoffCap = envDuration("BACKOFF_CAP", cfg.BackoffCap)
	st := store.New(pool, store.Config{OnTaskDone: engine.ObserveTaskDone})
	eng := engine.New(st, cfg)
	go eng.RunReaper(ctx)

	grpcServer := grpc.NewServer(grpc.MaxRecvMsgSize(maxMessageBytes), grpc.MaxSendMsgSize(maxMessageBytes))
	pb.RegisterEngineServer(grpcServer, server.New(eng))

	lis, err := net.Listen("tcp", grpcAddr)
	if err != nil {
		return err
	}

	mux := http.NewServeMux()
	host, _ := os.Hostname()
	mux.HandleFunc("/", server.StatusHandler(st, host))
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		pingCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := pool.Ping(pingCtx); err != nil {
			http.Error(w, "postgres: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte("ok\n"))
	})
	httpServer := &http.Server{Addr: httpAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	errc := make(chan error, 2)
	go func() { errc <- grpcServer.Serve(lis) }()
	go func() {
		if err := httpServer.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
			errc <- err
		}
	}()
	log.Printf("coordinator ready: grpc %s, http %s", grpcAddr, httpAddr)

	select {
	case <-ctx.Done():
		log.Printf("shutting down")
		grpcServer.Stop() // workers retry on the other coordinator
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return httpServer.Shutdown(shutdownCtx)
	case err := <-errc:
		return err
	}
}

// connect waits for Postgres, because the coordinator may start first.
func connect(ctx context.Context, url string) (*pgxpool.Pool, error) {
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(60 * time.Second)
	for {
		pingCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		err = pool.Ping(pingCtx)
		cancel()
		if err == nil {
			return pool, nil
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			pool.Close()
			return nil, err
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envDuration(key string, fallback time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		log.Fatalf("%s=%q is not a duration: %v", key, v, err)
	}
	return d
}

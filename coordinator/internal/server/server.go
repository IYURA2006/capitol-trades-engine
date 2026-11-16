// Package server maps gRPC requests onto the engine. It holds no logic.
package server

import (
	"context"
	"errors"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"capitoltrades/coordinator/internal/engine"
	"capitoltrades/coordinator/internal/pb"
	"capitoltrades/coordinator/internal/store"
)

type Server struct {
	pb.UnimplementedEngineServer
	engine *engine.Engine
}

func New(e *engine.Engine) *Server { return &Server{engine: e} }

func (s *Server) SubmitRun(ctx context.Context, req *pb.SubmitRunRequest) (*pb.SubmitRunResponse, error) {
	run := store.Run{ID: req.RunId, Workflow: req.Workflow, SnapshotID: req.SnapshotId, Seed: req.Seed}
	created, err := s.engine.SubmitRun(ctx, run, specs(req.Tasks))
	if err != nil {
		return nil, grpcError(err)
	}
	return &pb.SubmitRunResponse{Created: created}, nil
}

func (s *Server) LeaseTask(ctx context.Context, req *pb.LeaseRequest) (*pb.LeaseResponse, error) {
	leased, err := s.engine.LeaseTask(ctx, req.WorkerId, req.TaskTypes)
	if err != nil {
		return nil, grpcError(err)
	}
	if leased == nil {
		return &pb.LeaseResponse{HasTask: false}, nil
	}
	return &pb.LeaseResponse{
		HasTask: true, TaskId: leased.TaskID, TaskType: leased.Type, TaskKey: leased.Key,
		Payload: leased.Payload, Attempt: leased.Attempt,
		LeaseSeconds: int32(s.engine.LeaseDuration() / time.Second),
		RunId:        leased.RunID, SnapshotId: leased.SnapshotID, Seed: leased.Seed,
	}, nil
}

func (s *Server) Heartbeat(ctx context.Context, req *pb.HeartbeatRequest) (*pb.HeartbeatResponse, error) {
	alive, err := s.engine.Heartbeat(ctx, req.TaskId, req.Attempt)
	if err != nil {
		return nil, grpcError(err)
	}
	return &pb.HeartbeatResponse{LeaseLost: !alive}, nil
}

func (s *Server) Complete(ctx context.Context, req *pb.CompleteRequest) (*pb.CompleteResponse, error) {
	records := make([]store.Record, len(req.Records))
	for i, r := range req.Records {
		records[i] = store.Record{Kind: r.Kind, Key: r.Key, Data: r.Data}
	}
	outcome, err := s.engine.Complete(ctx, req.TaskId, req.Attempt, req.Output, records, specs(req.SubDag))
	if err != nil {
		return nil, grpcError(err)
	}
	if outcome == store.Stale {
		return &pb.CompleteResponse{Status: pb.CompleteResponse_STALE_LEASE}, nil
	}
	return &pb.CompleteResponse{Status: pb.CompleteResponse_OK}, nil
}

func (s *Server) Fail(ctx context.Context, req *pb.FailRequest) (*pb.FailResponse, error) {
	retryAfter := time.Duration(req.RetryAfterSeconds) * time.Second
	outcome, err := s.engine.Fail(ctx, req.TaskId, req.Attempt, req.Error, req.Fatal, retryAfter)
	if err != nil {
		return nil, grpcError(err)
	}
	if outcome == store.Stale {
		return &pb.FailResponse{Status: pb.FailResponse_STALE_LEASE}, nil
	}
	return &pb.FailResponse{Status: pb.FailResponse_OK}, nil
}

func specs(in []*pb.TaskSpec) []engine.Spec {
	out := make([]engine.Spec, len(in))
	for i, t := range in {
		out[i] = engine.Spec{
			Type: t.Type, Key: t.Key, Payload: t.Payload, DependsOn: t.DependsOnKeys,
			RateKey: t.RateKey, TolerateFailures: t.TolerateFailures, MaxAttempts: int(t.MaxAttempts),
		}
	}
	return out
}

// grpcError picks a status code that tells the client whether a retry can
// help. Unavailable = try again (maybe on the other coordinator).
func grpcError(err error) error {
	switch {
	case errors.Is(err, engine.ErrInvalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.Is(err, store.ErrDuplicateRecord):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, store.ErrNotFound):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, err.Error())
	default:
		return status.Error(codes.Unavailable, err.Error())
	}
}

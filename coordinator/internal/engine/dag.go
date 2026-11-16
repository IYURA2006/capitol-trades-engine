package engine

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"capitoltrades/coordinator/internal/store"
)

// Spec is one task as a client describes it. DependsOn holds "type/key"
// references to other specs in the same request.
type Spec struct {
	Type             string
	Key              string
	Payload          []byte
	DependsOn        []string
	RateKey          string
	TolerateFailures bool
	MaxAttempts      int
}

// TaskID is deterministic (design rule 6): the same run, type and key
// always give the same id, so inserting a task twice is harmless.
// The Python SDK computes the same value in capitol_sdk/ids.py.
func TaskID(runID, taskType, key string) string {
	sum := sha256.Sum256([]byte(runID + "\n" + taskType + "\n" + key))
	return hex.EncodeToString(sum[:])
}

// Ref is how one spec names another in DependsOn.
func Ref(taskType, key string) string { return taskType + "/" + key }

// BuildDAG validates specs and turns them into tasks with ids and parent ids.
// It rejects bad names, invalid JSON, duplicate keys, unknown dependencies
// and cycles.
func BuildDAG(runID string, specs []Spec) ([]store.NewTask, error) {
	index := make(map[string]int, len(specs))
	for i, s := range specs {
		if s.Type == "" || strings.ContainsAny(s.Type, "/\n") {
			return nil, fmt.Errorf("task %d: type %q must be non-empty without '/' or newline", i, s.Type)
		}
		if s.Key == "" || strings.Contains(s.Key, "\n") {
			return nil, fmt.Errorf("task %d: key %q must be non-empty without newline", i, s.Key)
		}
		if len(s.Payload) > 0 && !json.Valid(s.Payload) {
			return nil, fmt.Errorf("task %s: payload is not valid JSON", Ref(s.Type, s.Key))
		}
		ref := Ref(s.Type, s.Key)
		if _, dup := index[ref]; dup {
			return nil, fmt.Errorf("duplicate task %s", ref)
		}
		index[ref] = i
	}

	tasks := make([]store.NewTask, len(specs))
	children := make([][]int, len(specs)) // parent index -> child indexes
	waiting := make([]int, len(specs))    // unfinished parents, for the cycle check
	for i, s := range specs {
		ref := Ref(s.Type, s.Key)
		tasks[i] = store.NewTask{
			ID: TaskID(runID, s.Type, s.Key), Type: s.Type, Key: s.Key, Payload: s.Payload,
			RateKey: s.RateKey, TolerateFailures: s.TolerateFailures, MaxAttempts: s.MaxAttempts,
		}
		seen := make(map[string]bool, len(s.DependsOn))
		for _, dep := range s.DependsOn {
			if seen[dep] {
				continue
			}
			seen[dep] = true
			p, ok := index[dep]
			if !ok {
				return nil, fmt.Errorf("task %s depends on unknown task %s", ref, dep)
			}
			if p == i {
				return nil, fmt.Errorf("task %s depends on itself", ref)
			}
			children[p] = append(children[p], i)
			waiting[i]++
			tasks[i].ParentIDs = append(tasks[i].ParentIDs, TaskID(runID, specs[p].Type, specs[p].Key))
		}
	}

	// Kahn's algorithm: keep removing tasks that wait for nothing.
	// If some tasks are never removed, they form a cycle.
	var queue []int
	for i, n := range waiting {
		if n == 0 {
			queue = append(queue, i)
		}
	}
	removed := 0
	for len(queue) > 0 {
		i := queue[0]
		queue = queue[1:]
		removed++
		for _, c := range children[i] {
			waiting[c]--
			if waiting[c] == 0 {
				queue = append(queue, c)
			}
		}
	}
	if removed != len(specs) {
		return nil, fmt.Errorf("dependency cycle among %d tasks", len(specs)-removed)
	}
	return tasks, nil
}

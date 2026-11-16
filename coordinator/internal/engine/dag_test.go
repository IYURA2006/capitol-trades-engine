package engine

import (
	"strings"
	"testing"
	"time"
)

func TestBuildDAGRejectsBadInput(t *testing.T) {
	cases := []struct {
		name  string
		specs []Spec
		want  string
	}{
		{"unknown dependency", []Spec{{Type: "a", Key: "1", DependsOn: []string{"b/1"}}}, "unknown task"},
		{"duplicate key", []Spec{{Type: "a", Key: "1"}, {Type: "a", Key: "1"}}, "duplicate"},
		{"self dependency", []Spec{{Type: "a", Key: "1", DependsOn: []string{"a/1"}}}, "itself"},
		{"cycle", []Spec{
			{Type: "a", Key: "1", DependsOn: []string{"c/1"}},
			{Type: "b", Key: "1", DependsOn: []string{"a/1"}},
			{Type: "c", Key: "1", DependsOn: []string{"b/1"}},
		}, "cycle"},
		{"slash in type", []Spec{{Type: "a/b", Key: "1"}}, "type"},
		{"empty key", []Spec{{Type: "a", Key: ""}}, "key"},
		{"bad payload", []Spec{{Type: "a", Key: "1", Payload: []byte("{nope")}}, "JSON"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := BuildDAG("r", c.specs)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("err = %v, want it to mention %q", err, c.want)
			}
		})
	}
}

func TestBuildDAGDiamond(t *testing.T) {
	tasks, err := BuildDAG("r", []Spec{
		{Type: "d", Key: "1", DependsOn: []string{"b/1", "c/1", "b/1"}}, // order and repeats do not matter
		{Type: "b", Key: "1", DependsOn: []string{"a/1"}},
		{Type: "c", Key: "1", DependsOn: []string{"a/1"}},
		{Type: "a", Key: "1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := len(tasks[0].ParentIDs); got != 2 {
		t.Fatalf("d has %d parents, want 2", got)
	}
	if tasks[0].ParentIDs[0] != TaskID("r", "b", "1") {
		t.Fatal("parent id does not match TaskID")
	}
}

func TestTaskIDIsStableAndDistinct(t *testing.T) {
	// The Python SDK must produce exactly this value (sdk/tests/test_ids.py).
	// It is sha256("run-1\nparse_filing\n20025848").
	const want = "fba29246979c57810e9abd48918777b4565ff730fbd6a5ad85cbd8cc6db09229"
	got := TaskID("run-1", "parse_filing", "20025848")
	if got != want {
		t.Fatalf("TaskID = %s, want %s", got, want)
	}
	if got == TaskID("run-2", "parse_filing", "20025848") || got == TaskID("run-1", "parse", "filing/20025848") {
		t.Fatal("different inputs gave the same id")
	}
}

func TestBackoffGrowsAndIsCapped(t *testing.T) {
	base, limit := time.Second, 60*time.Second
	// r = 0.999... gives the top of the range, r = 0 the bottom (half).
	if got := Backoff(base, limit, 0, 0); got != 500*time.Millisecond {
		t.Fatalf("failures=0 r=0: %v, want 500ms", got)
	}
	prev := time.Duration(0)
	for f := 0; f < 6; f++ {
		d := Backoff(base, limit, f, 0.5)
		if d <= prev {
			t.Fatalf("backoff did not grow at failures=%d: %v after %v", f, d, prev)
		}
		prev = d
	}
	if got := Backoff(base, limit, 30, 0.999999); got > limit || got < limit/2 {
		t.Fatalf("failures=30: %v, want between %v and %v", got, limit/2, limit)
	}
}

package server

import (
	"context"
	"html/template"
	"net/http"
	"sort"
	"time"

	"capitoltrades/coordinator/internal/store"
)

var states = []string{"PENDING", "READY", "LEASED", "SUCCEEDED", "DEAD_LETTER", "CANCELLED"}

type statusRow struct {
	Type   string
	Counts []int
}

type statusView struct {
	*store.Status
	States []string
	Rows   []statusRow
	Totals []int
	Host   string
	Now    string
}

// The page is plain HTML that reloads itself every 2 seconds. No
// JavaScript and no state: it shows what Postgres says right now.
var statusPage = template.Must(template.New("status").Parse(`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Engine status</title>
<style>
 :root { --bg:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --line:#e4e3df; }
 @media (prefers-color-scheme: dark) { :root { --bg:#1a1a19; --ink:#f0efec; --muted:#c3c2b7; --line:#383835; } }
 body { font: 14px/1.45 system-ui, sans-serif; color: var(--ink); background: var(--bg); margin: 1.2rem auto; max-width: 1000px; padding: 0 16px; }
 h1 { font-size: 1.25rem; margin: 0; } h2 { font-size: 1rem; margin: 1.4rem 0 .4rem; }
 .muted { color: var(--muted); } .scroll { overflow-x: auto; }
 table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
 th, td { text-align: left; padding: .22rem .6rem; border-bottom: 1px solid var(--line); white-space: nowrap; }
 th { color: var(--muted); font-weight: 600; } td.n, th.n { text-align: right; }
 td.detail { white-space: normal; word-break: break-word; color: var(--muted); }
 a { color: inherit; } .sel { font-weight: 700; }
</style></head><body>
<h1>Engine status</h1>
<p class="muted">Coordinator {{.Host}} · {{.Now}} · reloads every 2 s · every number is read from Postgres</p>

{{if not .Runs}}<p>No runs yet. Submit one with <code>ctl submit</code>.</p>{{else}}
<h2>Runs</h2>
<div class="scroll"><table>
<tr><th>Run</th><th>Workflow</th><th>State</th><th>Snapshot</th><th>Created</th></tr>
{{range .Runs}}<tr><td><a href="/?run={{.ID}}" {{if eq .ID $.Selected}}class="sel"{{end}}>{{.ID}}</a></td><td>{{.Workflow}}</td><td>{{.State}}</td><td>{{.Snapshot}}</td><td>{{.Created}}</td></tr>
{{end}}</table></div>

<h2>Tasks in {{.Selected}}</h2>
<div class="scroll"><table>
<tr><th>Type</th>{{range .States}}<th class="n">{{.}}</th>{{end}}</tr>
{{range .Rows}}<tr><td>{{.Type}}</td>{{range .Counts}}<td class="n">{{.}}</td>{{end}}</tr>
{{end}}<tr><th>Total</th>{{range .Totals}}<th class="n">{{.}}</th>{{end}}</tr>
</table></div>

<h2>Dead letters{{if not .DeadLetters}} <span class="muted">(none)</span>{{end}}</h2>
{{if .DeadLetters}}<div class="scroll"><table>
<tr><th>Type</th><th>Key</th><th class="n">Attempt</th><th class="n">Failures</th><th class="n">Expiries</th><th>Last error</th></tr>
{{range .DeadLetters}}<tr><td>{{.Type}}</td><td>{{.Key}}</td><td class="n">{{.Attempt}}</td><td class="n">{{.Failures}}</td><td class="n">{{.Expiries}}</td><td class="detail">{{.Error}}</td></tr>
{{end}}</table></div>{{end}}

<h2>Recent events</h2>
<div class="scroll"><table>
<tr><th>Time</th><th>Task</th><th class="n">Attempt</th><th>Event</th><th>Detail</th></tr>
{{range .Events}}<tr><td>{{.At}}</td><td>{{.Type}}/{{.Key}}</td><td class="n">{{.Attempt}}</td><td>{{.Event}}</td><td class="detail">{{.Detail}}</td></tr>
{{end}}</table></div>
{{end}}
</body></html>
`))

// StatusHandler serves the read-only status page.
func StatusHandler(s *store.Store, host string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		status, err := s.ReadStatus(ctx, r.URL.Query().Get("run"))
		if err != nil {
			http.Error(w, "postgres: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		view := statusView{Status: status, States: states, Host: host,
			Now: time.Now().UTC().Format("15:04:05 UTC"), Totals: make([]int, len(states))}
		column := map[string]int{}
		for i, st := range states {
			column[st] = i
		}
		byType := map[string][]int{}
		for _, c := range status.Counts {
			if byType[c.Type] == nil {
				byType[c.Type] = make([]int, len(states))
			}
			byType[c.Type][column[c.State]] = c.Count
			view.Totals[column[c.State]] += c.Count
		}
		for taskType, counts := range byType {
			view.Rows = append(view.Rows, statusRow{Type: taskType, Counts: counts})
		}
		sort.Slice(view.Rows, func(i, j int) bool { return view.Rows[i].Type < view.Rows[j].Type })

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_ = statusPage.Execute(w, view)
	}
}

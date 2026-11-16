# 004: Sub-DAGs, rate limits, and no leader

**Decision 1: no leader.** Two coordinators run at the same time and neither is special. They keep nothing in memory; every request is one guarded transaction. Workers know both addresses and switch when a call fails.

*Alternative:* leader election with a lease and an epoch number. *Why not:* a leader is only needed to protect state that lives outside the database. There is none here, so the leader would add clock assumptions and buy nothing.

**Decision 2: a sub-DAG is inserted in the parent's `Complete` transaction.** The parent becomes SUCCEEDED and its children appear in the same commit. In the test a coordinator was killed while it was inserting 10,001 children: afterwards zero were visible, the worker retried on the other coordinator, and the run ended with exactly 10,002 tasks.

*Alternative:* insert children in batches. *Why not:* a crash between batches would leave half a DAG and a parent that looks finished.

**Decision 3: the rate limit is one row per key in Postgres.** A leaser locks the row with `FOR UPDATE SKIP LOCKED`, leases one task for that key, and moves `next_free_at` forward, all in one transaction.

*Alternative:* a token bucket in coordinator memory. *Why not:* with two coordinators each would have its own bucket and the real rate would double.

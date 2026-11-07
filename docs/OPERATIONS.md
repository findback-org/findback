# Running and recovering FindBack

The runtime uses the local `findback.db` as its durable inbox, domain ledger and
two delivery queues. Keep the private state directory, including this database,
when restarting. Configuration, claimant statements and staff evidence live
there; do not commit or share it.

## Inspect queue health

```sh
findback --state-dir .findback health
```

This opens an existing database read-only and reports one committed snapshot of
queue counts, due/scheduled deliveries, and fixed error codes. It does not load
credentials or output message bodies, account identities, destinations, delivery
IDs, or raw exception text. A missing state directory is not created.

`health.status` describes **durable queues only**. It does not establish that a
runtime process is running, connected to Switch, or able to reach Mattermost or
the model. A stopped runtime with empty queues can report `healthy`.
`attention_required` means a delivery is blocked or an inbox event failed;
`retry_scheduled` means a delivery has failed and remains eligible for automatic
retry. `due` is eligible work whose saved due time has arrived; `scheduled` is
eligible work waiting for that time. `persistent_retry_metadata: false` identifies
an older queue schema; the health command deliberately does not migrate it.

## Delivery retry contract

Both domain `outbox` notices and `runtime_messages` (including the conversational
showcase) use the same persistent policy:

- A transient failure saves a fixed error code and retries after 2, 4, 8, 16, 32,
  64 and 128 seconds. The eighth consecutive failure blocks the message with
  `RETRY_LIMIT`. The implementation also caps delays at 300 seconds.
- HTTP 401/403 blocks with `AUTHORIZATION_REQUIRED`; 404/410 with
  `DESTINATION_UNAVAILABLE`; 400/405/409/422 with `REQUEST_REJECTED`.
  These are definitive HTTP refusals, rather than uncertain POST outcomes.
  Rate limits and transient network/read failures use the transient policy.
- A lost POST response, POST transport failure, server 5xx, unreadable POST
  response, or incomplete operation response is an **unknown outcome**. Each
  subsequent attempt first reconciles the original delivery marker and expected
  agent in trusted room history. A matched post is recorded locally without
  posting again. This also covers cancellation or a crash after remote commit.
- Unusable, non-progressing or over-bound reconciliation history blocks with
  `RECONCILIATION_REQUIRED`. There is no fallback that blindly posts. The current
  reconciliation limit is 40 pages of up to 500 messages; operators must resolve
  incomplete history before retrying.
- Failed or blocked messages retain their original body, delivery ID and remote
  identity. Lifetime `attempts` counts completed delivery attempts, including
  reconciliation-only successes or read failures; it is not a count of HTTP
  POSTs. `consecutive_failures` tracks the current retry budget. `manual_retries`
  records explicit operator resets. Exception prose is never saved by this policy.

A pass selects up to 100 **due** rows from each queue. The domain outbox is
ordered by creation time and ID; runtime messages by insertion order. Future-due
and blocked rows do not consume the batch limit. The queues are serviced
sequentially within a pass, so a slow transport operation can still delay other
deliveries. There is no strict FIFO or cross-queue delivery-order guarantee:
new notices can pass a failed notice while it backs off. The authoritative state
and release authorization remain in SQLite, rather than notification order.

The showcase's review-ready notice waits for the exact comparison card to be
delivered. Waiting consumes no failure budget. A blocked card blocks its dependent
notice with `DEPENDENCY_BLOCKED`; repair and requeue both when appropriate.

Input processing is separate: at most four rooms execute concurrently, with at
most three nonstaff rooms so a slot remains available to the librarian. Input
events remain FIFO within each room. This does not impose output delivery order.

## Repair and explicitly requeue

Fix the cause first: restore access, correct the configured destination, recover
the transport, or establish a complete trusted reconciliation history. Inspect
private local records only from the authorized operator environment.

```sh
# Requeue failed/blocked deliveries in both queues, retaining all history.
findback --state-dir .findback retry-delivery

# Limit recovery to one queue, or to one exact locally inspected delivery ID.
findback --state-dir .findback retry-delivery --queue runtime
findback --state-dir .findback retry-delivery --queue outbox --id DELIVERY_ID
```

This command changes only `FAILED`/`BLOCKED` deliveries back to `PENDING`, resets
their consecutive failure budget and due time, and increments `manual_retries`.
It preserves lifetime attempts, body, ID, domain actions and all `SENT` rows. It
does not post, run a model, recreate approvals or bypass marker reconciliation.
The running runtime picks up due work on its next pass. A blocked dependency may
need recovery in both queues.

`retry-failed` is a different command: it requeues interrupted **inbox work**, not
notifications. Existing domain deduplication still applies, but use it only after
investigating the failed input. Neither recovery command authorizes an item release.

## Older input and source proof

An enrolled human's input is independently matched to the original Mattermost
post: event hash, immutable account ID, channel and exact body must agree;
webhook, bot, system, deleted and delegated input cannot authorize a human action.
The verifier uses the documented [channel-post API](https://docs.mattermost.com/api/reference/get-posts-for-channel)
with `page=0`, `per_page=200`, and a post-ID `before` cursor, for at most 20 pages.
It uses authoritative `order` IDs rather than extra thread material in `posts`.

Mattermost's [v11.9.0 implementation](https://github.com/mattermost/mattermost/blob/v11.9.0/server/channels/store/sqlstore/post_store.go)
filters `before` strictly by the anchor's creation timestamp. The verifier overlaps
the oldest timestamp boundary using a strictly newer anchor, so same-millisecond
posts are not skipped. It requires new IDs and valid descending timestamps on
each page. A full page containing only one timestamp cannot be traversed safely
with this strategy and is retained for operator attention.

Known wrong author/body/channel or prohibited post types are rejected without
mutation. Unavailable access/history, malformed or incomplete pages, nonprogressing
pagination, an absent source, or an exhausted search leave the original inbox
event `FAILED`, with a fixed `Provenance*` error class rather than provider prose.
They are not silently marked done. Restore proof/access before using
`retry-failed`; keep the original saved event and domain history intact.

Core 0.31 translates Mattermost root IDs into Switch thread IDs. This verifier
preserves the existing trusted Switch thread context and exact-card guards; it
does not independently prove that thread mapping against raw Mattermost `root_id`.
Comparing those unequal ID formats directly would reject genuine approvals.

## Activating this version and restarting

The new retry policy is loaded when the updated runtime starts; editing source
does not change an already running demo process. Stop the existing runner before
starting the updated one with the **same** private state directory and the same
launch command (`findback run` for command mode, or the existing showcase launcher
for that mode). Keep the database and delivery IDs intact. Startup additively
migrates queue metadata under a SQLite write transaction; an interrupted migration
rolls back and can be retried. Legacy exhausted outbox rows become blocked rather
than restarting an unlimited retry loop. Saved retry times survive restart.

The runner lock prevents two local processes using the same state directory.
Do not bypass it to speed up delivery. On shutdown, in-flight input work stays
pending and deliveries retain their durable state; a later start reconciles
uncertain remote effects before posting. Queue health can be inspected while a
runner is active, but it remains a queue snapshot rather than a liveness probe.

## Verification boundary

`tests/test_delivery.py` uses actual temporary SQLite databases and controlled
providers to exercise restart, committed-but-unacknowledged POSTs, cancellation,
permanent refusal, bounded retries, migration rollback and concurrent health reads.
These tests make no live Switch/Mattermost posts. The existing live demonstration
process is intentionally left unchanged while this version is verified; separate
read-only access checks do not prove that the new delivery policy was activated in
that older process.
`tests/test_provenance.py` exercises genuine older human approvals and community
reports through the real handlers, rejected tampering, safe failed-inbox recovery,
and a strict timestamp-cursor simulation, without live posts.

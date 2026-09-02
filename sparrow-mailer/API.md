# Sparrow API Doc

Served by `server.py run --api` on `127.0.0.1:8787` (`api_host` / `api_port` in
`campaign.toml`). Loopback only - for remote access use
`ssh -L 8787:localhost:8787 <host>` or Tailscale, never a `0.0.0.0` bind.

Contacts enter the system exclusively through this API.

## Auth

Bearer token from `SYSTEM_AUTH_TOKEN`, required on every route.

    T="Authorization: Bearer $SYSTEM_AUTH_TOKEN"
    J="Content-Type: application/json"

| code | meaning |
|---|---|
| `401` | missing or wrong token |
| `503` | `SYSTEM_AUTH_TOKEN` is not set on the server |
| `404` | unknown route, or unknown contact on `PATCH` |
| `409` | ambiguous: contact is in more than one campaign |

## Contact fields

`email` is required and is the identity key - lowercased, unique, compared
case-insensitively. These names are structural:

| field | notes |
|---|---|
| `email` | required, the upsert key |
| `firstname`, `lastname` | |
| `company`, `designation`, `phone` | |
| `campaign_id` | must equal `id` in `campaign.toml`, else `error`; omit and only the contact is touched |
| `status` | `active` `paused` `replied` `bounced` `done` `error` `unsubscribed` |
| `step` | index of the next step to send |
| `next_at` | UTC `YYYY-MM-DDTHH:MM:SSZ`; `null` means due immediately |

`campaign_id` is the stable key. `campaign.toml` also has a `name`, which is a
free-text label nothing keys off - rename it whenever you like.

**Every other key is kept as a merge field** and becomes `{{that_key}}` in
`campaign.toml`. Send `"city": "Pune"` and the copy can say `{{city}}`.

---

## POST /contacts

Upsert one object or an array of them. `201`.

    curl -s localhost:8787/contacts -H "$T" -H "$J" -d '{
      "email": "ada@acme.com", "firstname": "Ada", "lastname": "Lovelace",
      "company": "Acme", "designation": "Head of Ops", "phone": "+91 98765 43210",
      "campaign_id": "outreach", "city": "Pune" }'

    {"results": ["created"]}

An array applies in **one transaction** - 2000 rows in ~0.05s. Per-item failures
are reported positionally without failing the batch:

    {"results": ["created", "error: invalid email: 'garbage'"]}

Upsert semantics, which make replaying a source safe:

- Only the keys you send are written. Omitted keys keep their current values.
- Re-POSTing an existing contact **never** duplicates them, resets their `step`,
  or reactivates a `replied` / `done` enrollment.
- New merge fields merge into the existing ones rather than replacing them.

## PATCH /contacts/{email}

Same body semantics, but **never creates** - `404` if the address is unknown.

    curl -s -X PATCH localhost:8787/contacts/ada@acme.com -H "$T" -H "$J" \
      -d '{"status": "paused"}'

    {"result": "updated"}

`campaign_id` is inferred when the contact has exactly one enrollment. With
more than one you must name it, or you get `409 {"error": "specify campaign_id"}`.

Reschedule someone to restart at step 1 tomorrow morning:

    -d '{"status":"active", "step":1, "next_at":"2026-09-03T04:00:00Z"}'

There is no `DELETE` for contacts. Use `status: "unsubscribed"` or a
suppression, both of which keep the record of *why* they stopped.

## GET /contacts

    curl -s "localhost:8787/contacts?status=active&campaign_id=outreach&limit=50" -H "$T"

`status`, `campaign_id`, `email`, `limit` (default 200). Ordered by `next_at`.
Merge fields are flattened into each object alongside the structural ones. It is
a left join, so a contact with no enrollment appears with null campaign fields.

## POST /suppressions

A global block that outranks every enrollment - the send query excludes these
addresses regardless of status. Bounces add entries here automatically. `201`.

    curl -s localhost:8787/suppressions -H "$T" -H "$J" \
      -d '{"email":"ada@acme.com", "reason":"asked to stop"}'

## GET /events

Append-only log: `sent` `replied` `bounced` `error`, newest first,
`limit` default 100. This is the audit trail - enrollments hold current state,
events hold history.

    curl -s "localhost:8787/events?limit=20" -H "$T"

## GET /stats

    {
      "sent_today": 47, "max_per_day": 180, "budget_left": 133,
      "paused": false, "in_window": true, "due_now": 6,
      "contacts": 412, "suppressed": 3,
      "by_status": {"active": 180, "replied": 31, "done": 198}
    }

## PATCH /settings, DELETE /settings/{key}

Runtime overrides that beat `campaign.toml`, so you can change operations
without touching a file the copy lives in. Kill switch:

    curl -s -X PATCH localhost:8787/settings -H "$T" -H "$J" -d '{"paused": true}'

Useful keys: `paused`, `max_per_day`, `sends_per_tick`, `window_start`,
`window_end`. Delete one to fall back to `campaign.toml`:

    curl -s -X DELETE localhost:8787/settings/paused -H "$T"

Copy changes belong in `campaign.toml` where git can diff them. Operational
flips belong here, where they take effect on the next tick.

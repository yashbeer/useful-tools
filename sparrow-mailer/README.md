# sparrow

Single-file email campaign runner for any SMTP+IMAP mailbox. One tick per minute,
one email per tick, replies land in the real inbox because you send as the mailbox.

    server.py        everything: tick loop, SMTP, IMAP poller, HTTP API
    campaign.toml    authored copy + schedule (in git, hot-reloaded every tick)
    API.md           HTTP API reference
    sparrow.db       SQLite: contacts, enrollments, events, suppressions, settings

Contacts go in over the API only. Transport is env vars and `server.py` names no
provider: it refuses to start until `SMTP_HOST`, `IMAP_HOST` and `SMTP_PASS` are
set, so any SMTP+IMAP provider works and none is assumed.

No pip installs on Python 3.11+. On 3.10 it uses `tomli`, already present here.

## Running it locally

`server.py` loads `.env` from its own directory at startup, using systemd's
parsing rules so a local run and the unit see identical values. Anything already
in the real environment wins, so `SMTP_HOST=... python3 server.py ...` still
overrides the file. Point elsewhere with `--env path/to/file`.

`--dry-run` opens no sockets, so it needs no mailbox password at all. Terminal 1:

    cp .env.example .env       # fill in SMTP_HOST, SMTP_PASS, IMAP_HOST, SYSTEM_AUTH_TOKEN
    python3 server.py init
    python3 server.py run --loop --api --dry-run

Terminal 2 - contacts go in over the API:

    T="Authorization: Bearer $SYSTEM_AUTH_TOKEN"; J="Content-Type: application/json"

    curl -s localhost:8787/contacts -H "$T" -H "$J" -d '[
     {"email":"ada@acme.com","firstname":"Ada","lastname":"Lovelace",
      "company":"Acme","designation":"Head of Ops","campaign_id":"outreach"}]'

    curl -s localhost:8787/stats -H "$T"

Terminal 1 renders each due email in full, then goes quiet - it reprints only
when the copy or the queue actually changes, so an idle preview does not scroll:

    [dry] 1 due - quiet until campaign.toml or the queue changes

Edit `campaign.toml` or POST another contact and the next tick re-renders. No
restart, and nothing is ever mutated - the same contacts stay due indefinitely.

A preview is never gated on the send window, so you can rehearse copy at
midnight; it just notes what a real run would have done instead:

    note: a real run would idle here - outside send window 09:30-18:00 ... Asia/Kolkata

Speed up the feedback loop with `--interval 5`. Reset with `rm sparrow.db*`.

To watch real SMTP traffic without credentials or a real provider, point it at a
local catcher (`pipx install mailpit` or any `smtpd`) and drop `--dry-run`:

    SMTP_HOST=127.0.0.1 SMTP_PORT=1025 SMTP_SECURITY=none IMAP_HOST=off \
      python3 server.py run --loop --api

## Going live

`SMTP_HOST`, `IMAP_HOST` and `SMTP_PASS` are required - a real run checks them
at startup and exits listing whatever is missing, rather than failing at the
first send hours later. `IMAP_HOST=off` is an accepted answer, as is
`SMTP_SECURITY=none` in place of a password for a local relay. Ports, security
mode and the `*_USER` vars do have defaults, listed in `.env.example`.

| provider | `SMTP_HOST` | `IMAP_HOST` | |
|---|---|---|---|
| GoDaddy | `smtpout.secureserver.net` | `imap.secureserver.net` | |
| Gmail / Workspace | `smtp.gmail.com` | `imap.gmail.com` | app password, not your login |
| Microsoft 365 | `smtp.office365.com` | `outlook.office365.com` | basic auth is being retired |
| Zoho | `smtp.zoho.in` | `imap.zoho.in` | |
| Fastmail | `smtp.fastmail.com` | `imap.fastmail.com` | |
| Amazon SES | `email-smtp.<region>.amazonaws.com` | `off` | SES SMTP creds, not IAM keys |
| Postmark | `smtp.postmarkapp.com` | `off` | |
| local catcher | `127.0.0.1` (`SMTP_PORT=1025`, `SMTP_SECURITY=none`) | `off` | |

Write comments on their own line in the env file - systemd treats trailing text
after a value as part of the value.

`--dry-run` needs none of it - it opens no sockets, so the check is skipped.

    cp .env ~/.config/sparrow.env            # the unit reads this, not ./.env
    cp sparrow.service ~/.config/systemd/user/
    systemctl --user enable --now sparrow
    loginctl enable-linger $USER
    journalctl --user -u sparrow -f

Drop `--dry-run` and it sends for real. An idle daemon logs why once, on change:

    idle: outside send window 09:30-18:00 Mon,Tue,Wed,Thu,Fri Asia/Kolkata
    idle: paused (settings override)
    idle: daily budget spent (180)

Kill switch, no redeploy:

    curl -X PATCH localhost:8787/settings -H "$T" -H "$J" -d '{"paused": true}'

Bulk-load a JSON array in one transaction - 2000 rows takes ~0.05s:

    curl -s localhost:8787/contacts -H "$T" -H "$J" --data-binary @leads.json

## How sending works

- `sends_per_tick = 1` at 60s ticks — the tick *is* the rate limiter. A
  09:30-18:00 window is ~510 ticks, so `max_per_day = 180` never bunches up.
- GoDaddy allows 250 relays/day (each recipient counts as one), 500 with relay
  packs. 180 leaves headroom for normal mail from the same mailbox.
- Step 1 sends with a fresh `Message-ID`, stored as `thread_msgid`. Follow-ups
  set `In-Reply-To`/`References` to it and use `Re: <step 1 subject>`, so the
  sequence threads in the recipient's client.
- A step index past the end of `campaign.toml` clamps to the last step. Normal
  completion is decided at *send* time (`status='done'` in the same
  transaction), so the clamp only fires on real drift — nobody loops forever.
- `require_merge_fields` skips and flags a contact rather than sending "Hi ,".

## Replies and bounces

Every 5th tick, IMAP `SINCE` the last 2 days, headers only, `BODY.PEEK` so your
unread stays unread. Two ways to match, with different burdens of proof:

- **`In-Reply-To`/`References` names a `Message-ID` we sent** - conclusive, since
  that header cannot exist unless we mailed them first.
- **Sender address matches a contact** - only counts when we have actually sent
  to them (`last_sent_at IS NOT NULL`) and the mail arrived *after* that send,
  judged by IMAP `INTERNALDATE` rather than the forgeable `Date` header.

Without that second gate, any lead you had ever corresponded with would be
marked `replied` the moment you imported them, and would never be contacted. Mailer-daemon mail is scanned
for a known address and suppresses it. The scan is idempotent, so there is no
UID high-water mark to persist.

## API

Loopback only, bearer token, `--api` runs it inside the tick process. Contacts
enter the system exclusively this way. Full reference in **[API.md](API.md)**.

    T="Authorization: Bearer $SYSTEM_AUTH_TOKEN"; J="Content-Type: application/json"

    curl -s localhost:8787/contacts -H "$T" -H "$J" \
      -d '{"email":"a@b.com","firstname":"Ada","company":"Acme","campaign_id":"outreach"}'
    curl -s -X PATCH localhost:8787/settings -H "$T" -H "$J" -d '{"paused": true}'
    curl -s localhost:8787/stats -H "$T"

Remote access: `ssh -L 8787:localhost:8787 <host>` or Tailscale. Don't bind 0.0.0.0.

## Poking at it

    sqlite3 sparrow.db "SELECT campaign,status,count(*) FROM enrollments GROUP BY 1,2"
    sqlite3 -header -csv sparrow.db "SELECT * FROM contacts" > snapshot.csv
    pipx install datasette && datasette sparrow.db     # full browsable UI, free
    sqlite3 sparrow.db ".backup backups/$(date +%F).db"   # consistent while running

## Before real outreach

`makeflow.in` publishes SPF (`include:secureserver.net -all`) but no DKIM and no
DMARC. Add both, or Gmail and Outlook will be unkind.

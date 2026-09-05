# Inform Direct API client

Pulls company registry data (year ends, filing deadlines, officers, shareholders)
out of Inform Direct over its API, so the **Limited Companies Tracker / planner**
and **job reconciliation** stop depending on someone remembering to download a
CSV from the portfolio screen.

Two jobs it does:

1. **Registry feed** — writes the same `inform-direct-companies-<date>.csv` the
   tracker already reads from `Work Planners/Limited Companies Tracker/state/inform direct/`,
   straight from the API.
2. **Job reconciliation** — compares that registry against the planner's rows and
   tells you exactly which rows to snap to the registry, which to mark submitted,
   which to create, and which to put in front of a human.

Standard library only. No `pip install` needed to run it.

---

## Confirmed against the live API

| | |
|---|---|
| Sandbox base URL | `https://sandbox-api.informdirect.co.uk` |
| Production base URL | `https://api.informdirect.co.uk` |
| Auth request | `POST {base_url}/authenticate` with `{"apiKey": "..."}` |
| Auth response | `{"AccessToken": "<JWT>", "RefreshToken": "..."}` |
| Access token life | 15 minutes; `/refresh` on a 401, rotating both tokens |
| Request auth | `Authorization: Bearer {AccessToken}` |
| Operations | Get companies, Get company, Add company, Remove company |

The host and the key have to match: the production host answers a sandbox key
with 401, which is how the sandbox host was found. Response field names come
back PascalCase (`AccessToken`, not `access_token`); the parser matches field
names case- and punctuation-insensitively, so both work.

**Still unconfirmed**

- The literal paths for the four company operations, and the request body for
  Add company. `config/endpoints.json` is marked `"_status": "provisional"`
  until `fetch_spec` confirms them.
- Whether "high-level company details" includes the **accounts year end and
  filing deadline** — the thing the planner feed and reconciliation run on.
  `check` answers this in one command:

```
field coverage across the sample:
  [ok  ] company number                   25/25 (needed by the planner)
  [NONE] accounts due date                 0/25 (needed by the planner)
```

If those come back empty it is usually a naming mismatch, not an absent
feature — `companies --json` dumps each raw payload, and adding the real name to
the aliases in `models.py` makes it map.

**Officers, shareholders and filing history are not in the API.** The SA
director / close-company check cannot come off the browser. That code is written
and tested; the operations sit parked in `config/endpoints.json` under
`_not_offered_by_the_api`, and moving them into `operations` is all it would take
if a later version adds them. Until then `close_company_view` returns `None` for
them (as opposed to `[]`, which would mean "the API says there are none").

## Running it without sitting at the machine

The steps that need network access to Inform Direct can push their own results
back to the branch, so nobody has to relay terminal output:

```bash
export INFORMDIRECT_API_KEY="<sandbox key>"
./scripts/report.sh
```

It confirms the endpoint paths from the live spec, runs the field-coverage
check, writes `config/live-report.txt` and pushes it. Read-only against the API.
Company names and numbers are withheld from the report — only counts are kept —
and it refuses to push if the API key appears anywhere in the output.

## Where this has to run

This needs network access to `api.informdirect.co.uk`. A Claude Code session
started from the **web** (claude.ai/code) runs in a locked-down cloud container
whose egress allowlist does not include Inform Direct — the browser in that
container is in the cloud too, so driving it does not help. That is why the
setup below has not been run against the live API yet.

Run it from **Claude Code on the Mac** instead — the desktop app or the CLI —
where the network is yours and Claude can drive Chrome to log into Inform
Direct. The whole thing is then one command:

```bash
export INFORMDIRECT_BASE_URL="https://sandbox-api.informdirect.co.uk"
export INFORMDIRECT_API_KEY="<sandbox key>"
./scripts/setup.sh
```

`setup.sh` confirms the endpoint paths from the live spec, prints the resolved
settings, and reports whether the API returns the fields the planner needs. It
is read-only — adding and removing a company is `verify --confirm`, which it
tells you to run next.

(If web sessions should be able to reach Inform Direct, an admin can allowlist
the host for this environment — the block is an organisation egress policy, not
a limitation of the tool.)

## Setup

### 1. Generate a sandbox key

In the Inform Direct platform: **Account → Manage API keys → Add API Key**. That
gives you a sandbox-ready key for testing, with no effect on live data.

```bash
cp config/settings.example.json config/settings.json
$EDITOR config/settings.json      # gitignored
```

Or keep the key out of files entirely:

```bash
export INFORMDIRECT_BASE_URL="https://sandbox-api.informdirect.co.uk"
export INFORMDIRECT_API_KEY="..."
```

Sandbox and production are **separate hosts**, and each rejects the other's key
with a 401. Swap `base_url` to `https://api.informdirect.co.uk` when your
production key is enabled.

Environment wins over the file, and command line flags win over both.

### 2. Confirm the paths

Download the OpenAPI spec from SwaggerHub (**Export → Download API → JSON**) and
point the importer at it. It rewrites `config/endpoints.json` with the real paths
and tells you the correct `base_url`:

```bash
./scripts/grab_spec.sh
```

That probes every likely spec location, saves each response under
`config/spec-probe/` whether or not it finds one, updates `config/endpoints.json`
if it does, and pushes the lot — so a failed hunt still leaves enough to work out
the real path from anywhere. Underneath it is:

```bash
python3 scripts/fetch_spec.py --spec https://api.informdirect.co.uk --write
```

Given a bare host that tries the usual spec locations (`/swagger/v1/swagger.json`,
`/openapi.json` and friends) and, if none hit, reads the Swagger UI page to find where its spec actually
lives. Every candidate is parsed and validated before being accepted — this API
answers a missing path with HTTP 200 and an IIS error page, so a successful
fetch proves nothing. If the spec needs a key, pass
`--header 'X-Api-Key: ...'`. If it is not public at all, download it from
SwaggerHub (**Export → Download API → JSON**)
and pass the file instead:

```bash
python3 scripts/fetch_spec.py --spec ~/Downloads/informdirect.json --write
```

Add `--list` first to see every path before it decides. Anything it can't match
is named in the output — the file is four short entries, and nothing is generated
into code.

While you're in the docs, check the auth request shape against `auth_path`,
`refresh_path` and `api_key_field` in your settings. The defaults
(`/authenticate`, `/refresh`, `apiKey`) follow the documented description, and
`api_key_in: "auto"` tries the key in the JSON body then in a header, so there is
a fair chance it works untouched.

### 3. Check it works

```bash
python3 -m informdirect config    # resolved settings, secrets masked
python3 -m informdirect check     # gets a token, then reports field coverage
```

`check` exits `2` if a field the planner needs is missing, so it is safe to put
in a smoke test.

### 3b. If authentication fails

`authtest` reads the status codes as evidence: **401 means the request was
understood and the credential refused; 400 means it was not understood at all.**
That split names the right field without any documentation. Against the live API
it showed `{"apiKey": ...}` in a JSON body drawing 401 while every other field
name and every header drew 400 — so that shape is now the default, and a 401
points at the key or the host rather than the request.

When the shape is settled but the key is still refused, `authtest` retries the
same shape against the likely sandbox hosts and paths, and if none work it
drafts the question for Inform Direct support.


The auth endpoint is confirmed live — it answers HTTP 400, not 404 — but its
exact request shape is not documented publicly. `authtest` tries every plausible
shape once and shows the server's response to each:

```bash
python3 -m informdirect authtest
```

```
  [ 400 ] JSON body  {apiKey}     One or more validation errors occurred. -- apiKey: The apiKey field is required.
  [WORKS] header     X-Api-Key    ...
```

If one works it tells you exactly what to put in `config/settings.json`. If none
do, the validation messages name the field the server actually wants. It only
authenticates — nothing is added or removed.

### 4. Earn the production key

Inform Direct only enable a production key once their technical team have seen
successful sandbox calls to **all four** endpoints. `verify` makes exactly those
calls and reports each one:

```bash
python3 -m informdirect verify --confirm \
  --company-number 01234567 --auth-code AB12CD
```

```
  [pass] Authenticate     access token obtained
  [pass] Get companies    5 company(ies) returned
  [pass] Add company      linked 01234567
  [pass] Get company      01234567 Example Ltd
  [pass] Remove company   unlinked 01234567

All four endpoints returned successful authenticated responses.
To request production access, email support@informdirect.co.uk with:
  - your organisation name
  - last 6 of the sandbox key used here: ...123XYZ
  - last 6 of the production key you want activated
```

Add company and Remove company change the account, so they **only run with
`--confirm`** — without it the read-only half runs and the rest is reported as
skipped. `--keep` leaves the company linked instead of removing it. Point this at
sandbox, not production.

Then generate a production key in your account and send that email; their team
validate the sandbox calls before enabling it.

---

## Using it

### Registry feed for the planner

```bash
python3 -m informdirect companies \
  --out "/path/to/Work Planners/Limited Companies Tracker/state/inform direct/"
```

Given a directory it writes a dated file, which is exactly what the tracker's
daily run picks up (newest file wins). Given a filename it writes that file.
`--json` also saves a full snapshot including each company's raw API payload,
which is what you want when a field looks wrong.

Set it on a schedule and the tracker's "warn if the feed is >14 days old" check
never fires again.

### Job reconciliation

```bash
python3 -m informdirect reconcile \
  --planner planner-rows.csv \
  --out out/reconciliation.csv
```

`--planner` is any CSV with one row per job; headers are matched fuzzily, so
`Company Number` / `CRN` / `Registered Number` all work, as do
`Accounts Due Date` / `Deadline` / `Filing Deadline`. Add `--snapshot feed.csv`
to reconcile against a file you already exported instead of calling the API.

It applies the rules the tracker already uses:

| Situation | Action |
|---|---|
| Row's deadline year == registry's next-due year, dates differ | `snap_dates` — registry wins |
| Row's deadline year is earlier | `mark_submitted` — that period was filed |
| Registry's next-due year has no row | `create_row` |
| Registry says dissolved / struck off | `flag_dissolved` — never roll forward |
| In liquidation / administration | `flag_insolvent` |
| Planner row not in the portfolio | `flag_unmatched` |
| Row's deadline is *later* than the registry expects | `flag_row_ahead_of_registry` |
| Notes mention a charity | `charity_verify` — not a Companies House entity |

Exit codes: `0` nothing needing a human, `2` exceptions to look at, `1` an error.
So a scheduled run can alert only when it matters.

### One company

```bash
python3 -m informdirect company 01234567
```

Prints the company's high-level details. It also prints current directors and
shareholdings with percentages — the check the SA data run does by hand against
the Portfolio screen — but **only once those operations exist**; today they are
parked (see above) and that part comes back empty. Add `--json` to pipe it
somewhere.

### From Python

```python
from informdirect import Portfolio

portfolio = Portfolio()
for company in portfolio.iter_companies(include_dissolved=False):
    print(company.company_number, company.accounts_next_made_up_to,
          company.accounts_due)

# Company management (documented, but not exposed on the CLI)
portfolio.add_company("01234567", auth_code="AB12CD")   # CH authentication code
portfolio.remove_company("01234567")

# Officers and shareholders: written and tested, but the operations are parked
# in config/endpoints.json because the documented API does not offer them.
# Calling these today raises EndpointNotConfigured naming what is available.
view = portfolio.close_company_view("01234567")
```

---

## How it is put together

| File | What it does |
|---|---|
| `config.py` | Settings from args → env → JSON file → defaults; secrets masked on output |
| `auth.py` | API key → access token + rotating refresh token; also api_key and oauth2 modes |
| `transport.py` | Stdlib HTTP; swappable, which is how the tests avoid the network |
| `client.py` | Retries, backoff, error mapping, and pagination |
| `endpoints.py` | Named operations resolved through `config/endpoints.json` |
| `models.py` | `Company` / `Officer` / `Shareholder` with fuzzy field matching |
| `portfolio.py` | The high-level calls the planner and reconciler use |
| `export.py` | Writes the tracker's registry feed CSV |
| `reconcile.py` | Registry vs planner rows → a list of actions |
| `cli.py` | `python3 -m informdirect ...` |

Two deliberate choices, both because the paths and payload shapes are unconfirmed:

- **No URL path is hardcoded anywhere except `config/endpoints.json`.** Correcting
  that one file is the whole job of pointing this at the real API.
- **Field names are matched fuzzily**, the same way the tracker already matches
  spreadsheet headers. `nextAccountsMadeUpTo`, `next_accounts_made_up_to` and a
  nested `{"accounts": {"nextMadeUpTo": ...}}` all land in the same field, and the
  untouched payload stays on `.raw` so nothing is lost if a name surprises us.

The client also copes with whichever paging style the API turns out to use —
page numbers, offset/limit, cursors, `Link` headers or a `nextPageUrl` — and stops
safely if it turns out the API ignores paging parameters altogether.

## Tests

```bash
./run_tests.sh
```

196 tests, stdlib `unittest`, no network and no pip install.

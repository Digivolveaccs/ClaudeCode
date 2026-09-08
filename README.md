# Inform Direct API client

Client for Inform Direct's Integration API, built for the **Limited Companies
Tracker / planner** and **job reconciliation**.

The honest headline: **the API turned out to carry no dates**, so it cannot
replace the portfolio CSV export for filing deadlines. What it can do is answer
which client companies are actually linked to the Inform Direct account, and
link the ones that are missing. See [what this API can and cannot do](#what-this-api-can-and-cannot-do).

So there are two jobs here:

1. **Membership reconciliation** (works off the API) — which planner companies
   are in Inform Direct, which are not, and linking the missing ones.
2. **Deadline reconciliation** (works off the portfolio export) — compares a
   registry snapshot against the planner's rows and says which rows to snap to
   the registry, which to mark submitted, which to create, and which need a
   human. Written, tested, and ready for whenever the dates can be sourced.

Standard library only. No `pip install` needed to run it.

---

## What this API can and cannot do

Verified against the live sandbox, not inferred from documentation.

**A company record is three fields:**

```json
{"CompanyNumber": "01234567", "Name": "SANDBOX ONE LIMITED", "PublicUrl": "/c/abc"}
```

That is everything. No status, no accounts year end, no filing deadline, no
confirmation statement date, no incorporation date. There are no officer,
shareholder, filing or accounts endpoints — all confirmed 404.

| Wanted | Possible? | |
|---|---|---|
| Registry feed for the Limited Companies Tracker | **No** | the API has no dates to feed it |
| Deadline / job reconciliation | **No** | same reason |
| SA director & close-company check | **No** | no officers or shareholders endpoint |
| **Which companies are linked to the account** | **Yes** | `membership` |
| **Linking a new client company** | **Yes** | `membership --add`, or `add_company()` |

So the portfolio CSV export stays the source for deadlines. What this replaces
is the manual cross-check of *which* client companies are actually in Inform
Direct — a client missing from the portfolio is one nobody is filing for, and
that is now one command.

### Confirmed endpoints

| | |
|---|---|
| Sandbox | `https://sandbox-api.informdirect.co.uk` |
| Production | `https://api.informdirect.co.uk` (401s a sandbox key) |
| Auth | `POST /authenticate` `{"apiKey": "..."}` → `{"AccessToken", "RefreshToken"}` |
| Token life | 15 minutes; `Authorization: Bearer <token>`; `/refresh` on a 401, rotating both |
| Get companies | `GET /companies` → `{"Companies": [ ... ]}` |
| Get company | `GET /companies/{companyNumber}` → the same envelope, one entry |
| Add company | `POST /companies/add` `{"CompanyNumber", "AuthenticationCode"?}` → 201 |
| Remove company | `PUT /companies/delete` `{"CompanyNumber"}` → 200 |

The company number must be the full 8 characters; an unpadded one is rejected
with `400 The company number is invalid.` The client zero-pads automatically.

`POST /companies/add` refuses bulk payloads with
`429 "This end point is not meant for bulk uploading"`, so `membership --add`
paces its calls and stops on a 429.

**Remove is `PUT`, not `DELETE`.** `DELETE /companies/delete` answers 405 with
`allow: PUT`, which is what gave it away. Add and remove are collection actions
carrying the company number in the body rather than the path.

Add company returns 201 on success, 422 `Company already associated with this
account` when it is already linked (not a failure — the desired state holds),
404 when Companies House does not know the number, and 429 for a bulk payload.
The 201 message names an authentication code, so pass the Companies House code
with `--auth-code` when you have it: Inform Direct needs it before they can
file for the company.

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

### 3c. Production safety

Every command prints the environment it is talking to. Anything that changes the
portfolio — `verify --confirm`, `membership --add --confirm` — is **refused on
production unless you pass `--live`**. A host that is not recognisably a sandbox
counts as production, so a new or mistyped host errs towards being protected.
Read-only commands are unaffected.

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

### Company list export

Writes the tracker's feed CSV shape. **The date columns will be empty** — the
API does not supply them — so this is useful as a company list, not as the
deadline feed.

```bash
python3 -m informdirect companies \
  --out "/path/to/Work Planners/Limited Companies Tracker/state/inform direct/"
```

Given a directory it writes a dated file; given a filename it writes that file.
`--json` also saves a full snapshot including each company's raw API payload.

Do **not** point the tracker's `state/inform direct/` folder at this — the
tracker would read blank deadlines as authoritative. Keep using the manual
portfolio export for that folder.

### Membership — which companies are linked

```bash
python3 -m informdirect membership --planner planner-rows.csv --out out/report.csv
```

Compares the Inform Direct portfolio against your planner's companies and
reports both directions: rows missing from Inform Direct, companies linked but
absent from the planner, and name mismatches on the same number. Charity rows
are recognised and never expected in the portfolio. Company suffixes and case
are ignored when comparing names, so `Sandbox One Ltd` matches
`SANDBOX ONE LIMITED`.

Add the missing ones:

```bash
python3 -m informdirect membership --planner rows.csv --add --confirm --limit 25
```

Without `--confirm` it only says what it would do. Calls are paced and stop on a
rate limit.

### Job reconciliation (needs the portfolio export, not the API)

The API cannot supply deadlines, so this reads a registry snapshot from the
manual portfolio export. Everything below works — it just needs
`--snapshot` rather than a live call.


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

233 tests, stdlib `unittest`, no network and no pip install.

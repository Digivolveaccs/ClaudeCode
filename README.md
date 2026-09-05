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

## What is confirmed, and what is not

**Confirmed from Inform Direct's own documentation**

- **Endpoints**: Add company, Remove company, Get company, Get companies. That
  is the whole API.
- **Auth**: POST your API key to the authentication endpoint; you get back an
  **access token valid for 15 minutes** plus a **refresh token**. Send the
  access token as `Authorization: Bearer {token}` on every request. When a
  request returns 401, call `/refresh` with the stored refresh token — which
  returns a new access token *and a new refresh token*, so the stored one is
  rotated each time. Implemented as `auth_mode: "api_key_token"`, the default.
- **Keys**: sandbox keys are self-serve; production access is granted by Inform
  Direct's technical team after they validate your sandbox calls.

**Still unconfirmed**

- The literal endpoint paths. The host is `https://api.informdirect.co.uk`;
  whether sandbox is the same host (key-selected) or a separate one is not
  stated anywhere public.
- The exact JSON field names in requests and responses — including the body
  shape for Add company.

`config/endpoints.json` therefore ships marked `"_status": "provisional"` and
the CLI warns while that is true. Step 2 below clears it.

**The open question that matters**

The API returns *"high-level company details"*, and nothing published says
whether that includes the **accounts year end and filing deadline** — which is
exactly what the planner feed and the reconciliation run on. So `check` reports
it rather than leaving you to find out later:

```
field coverage across the sample:
  [ok  ] company number                   25/25 (needed by the planner)
  [ok  ] company name                     25/25 (needed by the planner)
  [NONE] accounts due date                 0/25 (needed by the planner)
```

If those fields are missing, it is usually a naming mismatch rather than an
absent feature — `companies --json` dumps each company's raw payload, and adding
the real name to the aliases in `models.py` makes it map.

**Officers, shareholders and filing history are not in the API.** So the SA
director / close-company check can't come off the browser. That code is written
and tested; the three operations sit parked in `config/endpoints.json` under
`_not_offered_by_the_api`, and moving them into `operations` is all it would
take if a later version adds them. Until then `close_company_view` returns
`None` for them (as opposed to `[]`, which would mean "the API says there are
none") and the CLI says so plainly.

---

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
export INFORMDIRECT_BASE_URL="https://api.informdirect.co.uk"
export INFORMDIRECT_API_KEY="..."
```

The key selects the environment — the same host serves sandbox and live, and
which one you get follows from the key you authenticate with. Worth confirming
against the docs; if there is a separate sandbox host, set `base_url` to that
while you are testing.

Environment wins over the file, and command line flags win over both.

### 2. Confirm the paths

Download the OpenAPI spec from SwaggerHub (**Export → Download API → JSON**) and
point the importer at it. It rewrites `config/endpoints.json` with the real paths
and tells you the correct `base_url`:

```bash
python3 scripts/fetch_spec.py --spec https://api.informdirect.co.uk --write
```

Given a bare host it tries the usual spec locations (`/swagger/v1/swagger.json`,
`/openapi.json` and friends) and tells you which one it found. If none of them
are public, download the spec from SwaggerHub (**Export → Download API → JSON**)
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

143 tests, stdlib `unittest`, no network and no pip install.

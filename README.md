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

## ⚠️ One thing is not finished yet

The published API docs
(`informdirect.portal.swaggerhub.com`) were **blocked by the network egress policy**
on the machine this was written on, so the exact **base URL, token URL and endpoint
paths could not be read**. Everything else — auth, retries, paging, parsing,
reconciliation — is written and tested.

`config/endpoints.json` therefore ships with `"_status": "provisional"` and
sensible-but-unconfirmed paths. The CLI prints a warning while that is the case.
See **Finishing the setup** below — it is about ten minutes of work.

---

## Setup

### 1. Credentials

Get an API client id and secret from Inform Direct (Settings → API, or via your
account manager — the API is a paid add-on on some plans).

```bash
cp config/settings.example.json config/settings.json
$EDITOR config/settings.json      # gitignored
```

Or keep the secret out of files entirely:

```bash
export INFORMDIRECT_BASE_URL="https://.../v1"
export INFORMDIRECT_TOKEN_URL="https://.../oauth2/token"
export INFORMDIRECT_CLIENT_ID="..."
export INFORMDIRECT_CLIENT_SECRET="..."
```

Every setting can come from either place; environment wins over the file, and
command line flags win over both. If Inform Direct issues a static API key rather
than OAuth client credentials, set `auth_mode` to `api_key` and fill in `api_key`
(and `api_key_header` if it is not `X-Api-Key`).

### 2. Finishing the setup

Download the OpenAPI spec from SwaggerHub (**Export → Download API → JSON**) and
point the importer at it. It rewrites `config/endpoints.json` with the real paths
and tells you the correct `base_url`:

```bash
python3 scripts/fetch_spec.py --spec ~/Downloads/informdirect.json --write
```

Add `--list` first if you want to see every path in the spec before it decides.
Anything it cannot match automatically is named in the output so you can fill it
in by hand — the file is five short entries, nothing is generated into code.

### 3. Check it works

```bash
python3 -m informdirect config    # resolved settings, secrets masked
python3 -m informdirect check     # gets a token and reads three companies
```

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

Prints the company, its current directors and its shareholdings with percentages
— the check the SA data run does by hand against the Portfolio screen. Add
`--json` to pipe it somewhere.

### From Python

```python
from informdirect import Portfolio

portfolio = Portfolio()
for company in portfolio.iter_companies(include_dissolved=False):
    print(company.company_number, company.accounts_next_made_up_to,
          company.accounts_due)

view = portfolio.close_company_view("01234567")
print([d.name for d in view["directors"]])
print([(s.name, s.percentage, s.is_corporate) for s in view["shareholders"]])
```

---

## How it is put together

| File | What it does |
|---|---|
| `config.py` | Settings from args → env → JSON file → defaults; secrets masked on output |
| `auth.py` | OAuth2 client credentials with token caching and refresh, or a static API key |
| `transport.py` | Stdlib HTTP; swappable, which is how the tests avoid the network |
| `client.py` | Retries, backoff, error mapping, and pagination |
| `endpoints.py` | Named operations resolved through `config/endpoints.json` |
| `models.py` | `Company` / `Officer` / `Shareholder` with fuzzy field matching |
| `portfolio.py` | The high-level calls the planner and reconciler use |
| `export.py` | Writes the tracker's registry feed CSV |
| `reconcile.py` | Registry vs planner rows → a list of actions |
| `cli.py` | `python3 -m informdirect ...` |

Two deliberate choices, both because the payload shapes were unconfirmed:

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

112 tests, stdlib `unittest`, no network and no pip install.

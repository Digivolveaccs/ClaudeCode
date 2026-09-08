# Inform Direct integration — where this stands

Last verified live against the sandbox: 5 September 2026.
**All four operations confirmed working.**

Verified read-only against **production: 8 September 2026** — the live key works
and the portfolio holds **404 companies**. Output in
[`config/production-check.txt`](config/production-check.txt).

## Confirmed endpoints

| | |
|---|---|
| Sandbox | `https://sandbox-api.informdirect.co.uk` |
| Production | `https://api.informdirect.co.uk` (each host refuses the other's key with 401) |
| Authenticate | `POST /authenticate` `{"apiKey": "..."}` → `AccessToken` + `RefreshToken`, 15 min |
| Get companies | `GET /companies` → `{"Companies": [...]}` |
| Get company | `GET /companies/{companyNumber}` → the same envelope, one entry |
| Add company | `POST /companies/add` `{"CompanyNumber", "AuthenticationCode"?}` → 201 |
| Remove company | `PUT /companies/delete` `{"CompanyNumber"}` → 200 |

Two things worth knowing, both found the hard way:

- **Remove is `PUT`, not `DELETE`.** `DELETE /companies/delete` answers 405
  with `allow: PUT`. Add and remove are collection actions carrying the number
  in the body, not the path.
- **Company numbers must be the full 8 characters.** An unpadded one is
  rejected with `400 The company number is invalid.` The client pads.

Add company responses:

| | |
|---|---|
| 201 | `Company added with no authentication code.` — Inform Direct needs the Companies House code before it can file |
| 422 | `Company already associated with this account.` — desired state already holds, not a failure |
| 404 | `Company could not be found.` — Companies House does not know that number |
| 429 | bulk payload refused; add one at a time |

## Proving it for production access

```bash
python3 -m informdirect verify --confirm --company-number <a real one>
```

Exercises all four in one run and leaves the account as it found it (adds, then
removes). Last run:

```
  [pass] Authenticate     access token obtained
  [pass] Get companies    1 company(ies) returned
  [pass] Add company      linked 05251849
  [pass] Get company      05251849 ADOREUM LTD
  [pass] Remove company   unlinked 05251849
```

## Production

The live key was activated by Inform Direct on 8 September 2026, and **verified
read-only the same day**.

```bash
export INFORMDIRECT_BASE_URL="https://api.informdirect.co.uk"
export INFORMDIRECT_API_KEY="<production key>"
python3 -m informdirect check --redact   # read-only; --redact withholds client names
```

Host and key are the only difference. `check`, `companies`, `company`,
`diagnose` and `membership` (without `--add`) are all read-only and safe there.

### What the live run showed

| | |
|---|---|
| Authenticate | works first time, same request shape as sandbox |
| Portfolio size | **404 companies** — distinct numbers and distinct names both 404 too, so no duplicates and no repeated page |
| Field coverage | company number and name on every record; status, next accounts made up to, accounts due date, last accounts made up to, confirmation statement due and incorporation date all absent |

**No behavioural difference from the sandbox — only scale** (sandbox held 1
company, production holds 404). Phase 2's richer company data has therefore not
appeared, so both conclusions above stand unchanged: the deadline feed still
needs the manual portfolio export, and the SA director / close-company check
stays in the browser.

Two things worth knowing when reading that output:

- `check` prints `sampled 25`. That is the command's own sample cap
  (`SAMPLE_SIZE` in `informdirect/cli.py`), **not** the portfolio total — the
  404 was counted by iterating the full paginated list.
- `config/production-check.txt` carries counts only. No client names, no company
  numbers, and the key masked.

The README says a web/cloud session cannot reach `api.informdirect.co.uk`
because of the egress allowlist. That is no longer true of this environment —
the host was reachable and both commands completed normally.

**Anything that changes the portfolio is refused on production unless you pass
`--live`:**

```
Refusing to add and remove a company against PRODUCTION (...) without --live.
  This changes the real Inform Direct portfolio.
```

That covers `verify --confirm` and `membership --add --confirm`. A host that is
not recognisably a sandbox counts as production, so a new or mistyped host errs
towards being protected. Every command now prints which environment it is
talking to.

Do not run `verify --confirm` against production casually — it adds and removes
a real company from the live portfolio. Use sandbox for that.

## What this API cannot do, and why

A company record is three fields: `CompanyNumber`, `Name`, `PublicUrl`. No
dates, no status, and no officer, shareholder or filing endpoints. So:

- the Limited Companies Tracker's **deadline feed still needs the manual
  portfolio export** — do not point `state/inform direct/` at this client;
- the **SA director / close-company check stays in the browser**.

Inform Direct have said richer company data is Phase 2, "could even allow you
to retrieve all your company details". Both code paths are written and tested
and start working if those fields appear; `check` reports it if they do.

## Housekeeping

- **Rotate the sandbox key** (Account → Manage API keys). The one used during
  setup was shared in a chat transcript.

# Inform Direct integration — where this stands

Last verified live against the sandbox: 5 September 2026.

## Working

| | |
|---|---|
| Authenticate | `POST /authenticate` `{"apiKey": "..."}` → `AccessToken` + `RefreshToken`, 15 min |
| Get companies | `GET /companies` → `{"Companies": [...]}` |
| Get company | `GET /companies/{companyNumber}` → same envelope, one entry |
| Membership reconciliation | `informdirect membership --planner rows.csv` |

Sandbox `https://sandbox-api.informdirect.co.uk` · production
`https://api.informdirect.co.uk` (each refuses the other's key with a 401).

## Waiting on Inform Direct

Emailed support@informdirect.co.uk, 5 September 2026:

1. **Remove company endpoint.** Not located — every candidate answers 405 with
   `allow: GET`. When they reply, add it to `operations` in
   `config/endpoints.json` and `remove_company()` works unchanged.
2. **A company number that Add company accepts in sandbox.**
   `POST /companies/add` authenticates and reaches the handler, but every test
   number returns `404 Company could not be found` — it looks the company up at
   Companies House, and `01234567` is not real. A real number should work.
3. **Production key**, once they have validated the sandbox calls.

## When the production key is enabled

```bash
export INFORMDIRECT_BASE_URL="https://api.informdirect.co.uk"
export INFORMDIRECT_API_KEY="<production key>"
python3 -m informdirect check
```

Nothing else changes — the host and key are the only difference.

## What this cannot do, and why

The company record is three fields: `CompanyNumber`, `Name`, `PublicUrl`. There
are no dates and no officer, shareholder or filing endpoints. So:

- the Limited Companies Tracker's **deadline feed still needs the manual
  portfolio export** — do not point `state/inform direct/` at this client;
- the **SA director / close-company check stays in the browser**.

Both code paths are written and tested and start working if those fields ever
appear. `check` reports it if they do.

## Housekeeping

- **Rotate the sandbox key** (Account → Manage API keys). The one used during
  setup was shared in a chat transcript.

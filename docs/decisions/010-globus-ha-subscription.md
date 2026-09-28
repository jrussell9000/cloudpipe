# 010 — UW-Madison High Assurance Globus subscription

**Status**: Accepted

## Context

The ABCD source data lives in the DAIRC MMPS Globus collection (`<YOUR_GLOBUS_SOURCE_COLLECTION_ID>`). This collection is a **Globus High Assurance (HA) collection**. HA collections enforce stricter authentication requirements because they hold sensitive or regulated data (ABCD data is subject to a data use agreement and is not publicly accessible).

Globus HA collections require that **both** endpoints in a transfer be HA. If the cloudpipe destination collection is not HA, Globus rejects transfers from the HA source with a permission error at the collection level, regardless of whether the individual user has transfer access.

HA status requires a Globus subscription from an organization with a High Assurance subscription tier. Individual Globus accounts cannot create HA endpoints.

## Decision

Subscribe the cloudpipe GCS v5 endpoint to the UW-Madison High Assurance subscription (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`). This grants the cloudpipe endpoint HA status, allowing it to participate in transfers with the DAIRC MMPS HA source collection.

Subscription is requested once via `globus-connect-server endpoint set-subscription-id` from the GCS instance (documented in `docs/globus-setup.md`). The subscription is tied to the GCS endpoint UUID, not the EC2 instance — it survives instance replacement.

The subscription also enables the S3 storage gateway add-on (ADR 001), which is included in the UW-Madison HA tier.

## Consequences

- HA authentication requirements apply to the `cloudpipe-transfer` native app: `--authentication-timeout-mins` is set to 1 week (max 30 days for HA endpoints) — *the parenthesised ceiling is wrong; see the Correction below*
- `setup_auth.py` uses `prompt=login` to force a fresh authentication event; reusing an existing browser session can produce a `No effective ACL rules` 403 on HA collections
- The refresh token expires after up to 30 days. When it expires, all transfers fail with an auth error and `setup_auth.py` must be re-run interactively. There is no automated token rotation. — *this attributes the lapse to the wrong mechanism; see the Correction below*
- Globus subscription status is tied to the UW-Madison institutional account. If the institution's Globus subscription lapses or the cloudpipe endpoint is removed from the subscription, the cloudpipe collection loses HA status and transfers from the DAIRC source will fail
- The GCS endpoint UUID (`<YOUR_GLOBUS_ENDPOINT_ID>`) and collection UUID (`<YOUR_GLOBUS_DEST_COLLECTION_ID>`) must be preserved across instance replacements — which the `gcs-auto-reregister` boot script and `lifecycle { ignore_changes }` on the SSM parameter ensure (see `docs/globus.md`)

## Correction (2026-09): there is no 30-day High Assurance ceiling

Two consequences above were wrong, and together they made a self-inflicted
weekly chore look like a Globus constraint. **The decision to subscribe to HA is
unaffected** — only its stated consequences were misdescribed.

Measured 2026-09-15 through the Transfer API's `get_endpoint`:

| | `cloudpipe-s3` (ours) | NBDC Datashare ABCD Release (source) |
|---|---|---|
| high assurance | true | true |
| `authentication_timeout_mins` | 10080 (7 days) | **525600 (1 year)** |

**There is no 30-day maximum.** A High Assurance collection we transfer with
every day is configured an order of magnitude above the supposed ceiling, which
a real limit would forbid. Our 7-day cadence is a value chosen when
`cloudpipe-s3` was created, not a rule imposed on us.

**The weekly failure is the gateway's session timeout, not refresh-token
expiry.** The cadence matched `10080` exactly, and `storage-gateway update s3`
accepts `--authentication-timeout-mins` (only `--high-assurance` is immutable).
The conflation mattered operationally: it pointed whoever hit the failure at
rotating a credential, when the fix was a one-line change to a gateway setting.

The refresh token's own lifetime was never measured, so no replacement figure is
asserted here — only that it is not what was forcing the weekly re-login.

`simplify-globus-ingress` raises the production gateway to `43200` (30 days) and
declares it in version-controlled configuration so it cannot drift back.

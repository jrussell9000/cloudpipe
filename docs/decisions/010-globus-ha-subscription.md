# 010 — UW-Madison High Assurance Globus subscription

**Status**: Accepted

## Context

The ABCD source data lives in the DAIRC MMPS Globus collection (`43583c7d-29c9-4d36-9cb5-c8a1641923cb`). This collection is a **Globus High Assurance (HA) collection**. HA collections enforce stricter authentication requirements because they hold sensitive or regulated data (ABCD data is subject to a data use agreement and is not publicly accessible).

Globus HA collections require that **both** endpoints in a transfer be HA. If the cloudpipe destination collection is not HA, Globus rejects transfers from the HA source with a permission error at the collection level, regardless of whether the individual user has transfer access.

HA status requires a Globus subscription from an organization with a High Assurance subscription tier. Individual Globus accounts cannot create HA endpoints.

## Decision

Subscribe the cloudpipe GCS v5 endpoint to the UW-Madison High Assurance subscription (`<YOUR_GLOBUS_SUBSCRIPTION_UUID>`). This grants the cloudpipe endpoint HA status, allowing it to participate in transfers with the DAIRC MMPS HA source collection.

Subscription is requested once via `globus-connect-server endpoint set-subscription-id` from the GCS instance (documented in `docs/globus-s3-gateway-config.md`). The subscription is tied to the GCS endpoint UUID, not the EC2 instance — it survives instance replacement.

The subscription also enables the S3 storage gateway add-on (ADR 001), which is included in the UW-Madison HA tier.

## Consequences

- HA authentication requirements apply to the `cloudpipe-transfer` native app: `--authentication-timeout-mins` is set to 1 week (max 30 days for HA endpoints)
- `setup_auth.py` uses `prompt=login` to force a fresh authentication event; reusing an existing browser session can produce a `No effective ACL rules` 403 on HA collections
- The refresh token expires after up to 30 days. When it expires, all transfers fail with an auth error and `setup_auth.py` must be re-run interactively. There is no automated token rotation.
- Globus subscription status is tied to the UW-Madison institutional account. If the institution's Globus subscription lapses or the cloudpipe endpoint is removed from the subscription, the cloudpipe collection loses HA status and transfers from the DAIRC source will fail
- The GCS endpoint UUID (`8e6a5497-c260-4613-8698-e4fecd6365eb`) and collection UUID (`00666689-6b52-444d-b6a8-57a3ce6ee97c`) must be preserved across instance replacements — which the `gcs-auto-reregister` boot script and `lifecycle { ignore_changes }` on the SSM parameter ensure (see `docs/globus.md`)

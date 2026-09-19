# Globus ingress — prerequisites

[globus-setup.md](globus-setup.md) tells you how to build the Globus ingress.
This page tells you what you must already have before those steps can work at
all, and **three of the four things on this list cannot be obtained from this
repository, by Terraform, or by any amount of automation.** They are approvals
and subscriptions granted by other organisations, on their timelines.

Read this before starting. Each gate below can take days to weeks, several run
in parallel, and discovering gate 2 after completing gate 1 is a common and
avoidable way to lose a month.

> **You may not need any of this.** If you already have ABCD minimally
> preprocessed data, or can get it by another route, the pipeline does not
> require Globus — it requires objects at known S3 keys. See
> [data-ingress.md](data-ingress.md).

---

## Summary

| Gate | What | Who grants it | Typical time | Can it be automated? |
|---|---|---|---|---|
| 1 | ABCD data access (NDA Data Use Certification) | NIMH Data Archive + your institution's signing official | Weeks | No |
| 2 | Globus subscription, **High Assurance** tier | Globus, via your institution | Days–weeks (or already exists) | No |
| 3 | Your endpoint added to that subscription | A human Globus admin at your institution | Hours–days | No |
| 4 | The GCS build itself | You | ~1 day | Partly — two browser flows are irreducible |

Gates 1 and 2 are independent — start both at once. Gate 3 requires gate 2 and
an endpoint UUID from the first half of gate 4. Gate 4's storage-gateway step
cannot complete until gate 3 is done.

```
Gate 1 (NDA DUC) ─────────────────────────┐
                                          ├──> transfers work
Gate 2 (HA subscription) ──> Gate 3 ──────┤
                              ↑           │
Gate 4a (endpoint setup) ─────┘           │
                                          │
                   Gate 4b (gateway, collection, credentials, token) ──┘
```

---

## Gate 1 — ABCD data access

**What it is.** ABCD data is distributed by the [NIMH Data Archive
(NDA)](https://nda.nih.gov). Access is granted per-investigator under a **Data
Use Certification (DUC)**, not per-institution and not per-project. The
minimally preprocessed imaging data that CloudPipe consumes (Hagler et al.
2019) is prepared by the ABCD **DAIRC** and served from a Globus collection.

**Why it gates everything.** The DAIRC collection is a Globus High Assurance
collection precisely *because* it holds DUC-governed human subjects data. No
DUC, no access to the collection, regardless of what infrastructure you build.

**How to obtain it.**

1. Create an NDA account at [nda.nih.gov](https://nda.nih.gov).
2. Identify your institution's **signing official** — the person authorised to
   sign data use agreements on the institution's behalf. This is usually in the
   sponsored programs or research compliance office, not in your lab or
   department.
3. Confirm your institution has an active **Federalwide Assurance (FWA)**. NDA
   requires one; an institution without it cannot sign.
4. Submit the DUC for the ABCD collection through the NDA portal and have the
   signing official countersign.
5. Once approved, request access to the DAIRC minimally preprocessed imaging
   share and ask the DAIRC how they grant Globus access for your NDA identity.

**How to verify you cleared it.** Log into [app.globus.org](https://app.globus.org)
with the identity your DUC is associated with, search the collection list for
the DAIRC MMPS collection by name, and browse it. If you can list a subject
directory, gate 1 is clear. Note the collection's UUID from its overview page —
this becomes `globus_source_collection_id` in `terraform.tfvars`, and the path
you browsed to becomes `globus_source_base_path`.

**What failure looks like.** The collection is not searchable, or browsing it
returns a permissions error rather than a listing. Both mean the access grant
has not propagated to the identity you are logged in as. Check that you are
logged in with the *same* identity your DUC names — a personal Globus account
and an institutional one are different identities even with the same email.

**Timeline.** Weeks is normal. The institutional signature is usually the long
pole, and it is not something you can expedite from your side.

> The NDA and DAIRC own this process and change it independently of CloudPipe.
> Their documentation is authoritative; treat the steps above as orientation,
> not as a specification.

---

## Gate 2 — A Globus subscription with High Assurance

**What it is.** Globus sells tiered subscriptions to institutions. The **High
Assurance** capability — required to create an HA endpoint — is a paid tier
feature.

**Why it gates everything.** From [ADR 010](decisions/010-globus-ha-subscription.md):

> Globus HA collections require that **both** endpoints in a transfer be HA. If
> the cloudpipe destination collection is not HA, Globus rejects transfers from
> the HA source with a permission error at the collection level, regardless of
> whether the individual user has transfer access.

And the sentence that determines whether this project is reproducible for you
at all:

> **Individual Globus accounts cannot create HA endpoints.**

There is no personal, self-service, or pay-as-you-go route to an HA endpoint.
You need an institution that holds a Globus subscription at the HA tier and is
willing to sponsor your endpoint under it.

**A second thing the subscription buys.** The GCS **S3 connector** used by the
production deployment is also a subscription feature. If your institution's
subscription includes HA but not the S3 connector, you are not blocked — set
`globus_use_s3_gateway = false` and use the POSIX+EBS staging path
([globus-setup.md → Appendix](globus-setup.md#appendix-posixebs-staging-historical)).
It is slower and adds an `aws s3 sync` step, but it needs no S3 add-on and no
static IAM user key.

**How to obtain it.**

1. Ask your institution's research computing or IT group whether a Globus
   subscription already exists, and at which tier. Most R1 universities have
   one; many researchers do not know it.
2. Find the Globus administrators' contact address (commonly a role account
   such as `globusadmins@<your-institution>`). Your research computing help
   desk will know it.
3. Ask them, specifically: *"Does our subscription include the High Assurance
   tier, and does it include the S3 connector?"* Those are two separate
   answers and you need both.
4. Record the **subscription UUID** they give you — you need it at gate 3.

**How to verify you cleared it.** You have a subscription UUID and a written
confirmation that it includes HA. There is nothing to test yet; the test is
gate 3.

**What failure looks like.** If the subscription is missing or lacks HA, the
failure appears much later — `globus-connect-server storage-gateway create s3
--high-assurance` fails with a subscription error at
[globus-setup.md Step 3](globus-setup.md#step-3-s3-storage-gateway). This is
why gate 2 is worth confirming in writing before you build anything.

**If your institution has no HA subscription.** You have three honest options,
and none of them is "work around it":

- Have your institution purchase the tier (a procurement conversation, not a
  technical one).
- Find a collaborating institution that has one and will sponsor an endpoint.
- Obtain the data by a non-Globus route and use pre-staged S3 ingress
  ([data-ingress.md](data-ingress.md)). This is the option most people outside
  a subscribing institution should take.

---

## Gate 3 — Your endpoint added to the subscription

**What it is.** The subscription is applied to a *specific endpoint UUID* by a
Globus administrator at the subscribing institution. It is not something you
can self-serve.

**Why it is a separate gate.** It requires an endpoint UUID, which does not
exist until you have run the first half of gate 4 (`endpoint setup`). So the
build order is: create the endpoint → request the subscription → then continue
with the storage gateway.

**How to obtain it.**

1. Complete [globus-setup.md Steps 1–2](globus-setup.md#step-1-wait-for-user_data-to-complete)
   to create the endpoint and obtain its UUID.
2. Email your institution's Globus administrators with the endpoint UUID and
   request it be added to the subscription
   ([globus-setup.md Step 7](globus-setup.md#step-7-subscribe-the-endpoint-to-the-uw-madison-ha-subscription)).
3. Wait for confirmation before attempting the storage-gateway step.

**How to verify you cleared it.** Run `globus-connect-server endpoint show` on
the GCS instance and confirm a subscription ID is present.

**What failure looks like.** Gateway creation fails with a subscription error,
as in gate 2.

> **The subscription is tied to the endpoint UUID, not the EC2 instance.** This
> matters operationally: routine instance replacement preserves the UUID (the
> `gcs-auto-reregister` boot service re-registers the node against the existing
> endpoint), so you do not re-request. But if the *endpoint* is ever deleted
> and recreated, you must go back to your Globus admins with the new UUID. See
> [globus.md → Endpoint recovery](globus.md#endpoint-recovery-after-accidental-deletion).

---

## Gate 4 — The build itself

With gates 1–3 cleared, [globus-setup.md](globus-setup.md) is the ordered
procedure. This section covers only what that guide assumes you already
decided.

### What you must register by hand, in a browser

Four distinct Globus registrations are involved, and confusing them is the most
common setup error. They are not interchangeable:

| # | Registration | Type | Created at | Used for |
|---|---|---|---|---|
| 1 | `cloudpipe-gcs` | Thick client / script | [developers.globus.org](https://developers.globus.org) | Owns the endpoint. Becomes `globus_client_id` in `terraform.tfvars` |
| 2 | `cloudpipe-gcs-mgmt` | Service account / confidential client | [app.globus.org/settings/developers](https://app.globus.org/settings/developers) | Non-interactive `globus-connect-server` management. Without it, admin commands break every ~30 days |
| 3 | `cloudpipe-transfer` | **Native app** | [developers.globus.org](https://developers.globus.org) | The Argo transfer step's refresh token |
| 4 | IAM user access key | AWS, not Globus | AWS IAM | Registered into the S3 gateway so GridFTP can write to S3 |

Using registration 1's UUID where registration 3 is expected fails with *"Only
native app clients can access this URL"* — a confusing error whose cause is
simply the wrong app type.

**Two browser flows are irreducible.** `endpoint setup` and
`globus-connect-server login localhost` both require an interactive
authentication in a browser, as does `setup_auth.py` (with `prompt=login`
forced, because reusing an existing browser session can produce a
`No effective ACL rules` 403 on an HA collection). These are Globus security
controls that exist deliberately. Do not build automation around them.

### Why the S3 gateway needs a static IAM user key

The GCS S3 connector has **no IMDS / EC2 instance role support**. It supports
exactly three credential modes, and only one of them works for an automated
pipeline: `--s3-user-credential` combined with `--admin-managed-credentials`,
where the admin registers a single IAM *user* access key that applies to all
mapped identities.

This means a long-lived static key exists by necessity, and rotating it is a
manual operational task. Note also that AWS Mountpoint for S3 is not an
alternative — it only supports sequential writes from byte 0, while GridFTP's
Extended Block Mode writes out of order, producing
`500 globus_xio: System error in write: Invalid argument`.

### Decisions to make before you start

| Decision | Variable | Guidance |
|---|---|---|
| S3 gateway or POSIX staging? | `globus_use_s3_gateway` | `true` if your subscription includes the S3 connector; otherwise `false` |
| Which identity domain may authenticate? | `globus_identity_domain` | Your institution's domain. Passed as `--domain` at gateway creation |
| Who owns the endpoint? | `globus_owner_email` | An institutional Globus admin, not a personal account — personal ownership breaks when the person leaves |
| Source collection and path | `globus_source_collection_id`, `globus_source_base_path` | From gate 1 |
| Which SSH sources are allowed? | `globus_admin_prefix_list_id` | An AWS managed prefix list. Ports 443 and 50000–51000 **must** be open to `0.0.0.0/0` — GridFTP data channels are peer-to-peer worldwide and access control is enforced by Globus OAuth2, not by network filtering |

---

## The ongoing manual burden

Setup is one-time; these are not. Anyone evaluating whether to reproduce this
ingress should weigh the recurring cost, because it is the part that actually
hurts.

| Task | Frequency | Automatable? | Symptom when missed |
|---|---|---|---|
| Re-run `setup_auth.py` for a fresh refresh token | Up to every 30 days | **No** — HA requires an interactive `prompt=login` | All transfers fail with an auth error |
| Re-authenticate the HA session | Weekly (`--authentication-timeout-mins` is set to 1 week) | No | Presents as `not_from_allowed_domain` — *not* as an expiry message |
| Rotate the S3 gateway IAM access key | Per your security policy | Partly | `invalid_credential` / *"Your credential requires some initial setup"* |
| Re-request subscription after endpoint deletion | Only after endpoint loss | No | Gateway operations fail with a subscription error |

The weekly session lapse is worth calling out separately: it does **not**
announce itself as an expiry. It surfaces as a domain-restriction error, which
sends people looking at `--domain` and identity mapping instead of at token
age. Check token age first.

Rotation procedures are in
[globus.md → Credential rotation](globus.md#credential-rotation).

---

## Decision: should you reproduce this?

Answer these in order. The first "no" is your answer.

1. **Do you have, or can you obtain, an approved NDA DUC for ABCD?**
   No → you cannot use this data at all. Nothing else matters.
2. **Are you pulling directly from the DAIRC Globus collection?**
   No → use pre-staged S3 ingress ([data-ingress.md](data-ingress.md)). Skip
   Globus entirely.
3. **Does your institution hold a Globus HA subscription and will it sponsor
   your endpoint?**
   No → obtain the data another way and pre-stage it. Building the Globus path
   is not possible.
4. **Can you absorb an interactive credential refresh at least monthly?**
   No → pre-stage in bulk instead of running Globus continuously.

If all four are yes, [globus-setup.md](globus-setup.md) will work for you, and
the deployment values it expects are listed in
[terraform.tfvars.example](https://github.com/jrussell9000/cloudpipe/blob/main/terraform/terraform.tfvars.example).

---

## Related

- [data-ingress.md](data-ingress.md) — the S3 contract Globus exists to satisfy, and the alternatives
- [globus-setup.md](globus-setup.md) — the ordered build procedure
- [globus.md](globus.md) — operating the running system
- [ADR 001](decisions/001-s3-gateway-over-posix-staging.md) — why the S3 gateway over POSIX staging
- [ADR 010](decisions/010-globus-ha-subscription.md) — why High Assurance is mandatory

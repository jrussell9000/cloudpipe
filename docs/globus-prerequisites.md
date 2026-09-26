# Globus ingress — prerequisites

[globus-setup.md](globus-setup.md) tells you how to build the Globus ingress.
This page tells you what you must already have before those steps can work at
all, and **all but one of the things on this list cannot be obtained from this
repository, by Terraform, or by any amount of automation.** They are approvals,
subscriptions, and account permissions granted by other organisations or other
teams, on their timelines.

Read this before starting. Each item below can take days to weeks, several run
in parallel, and discovering gate 2 after completing gate 1 is a common and
avoidable way to lose a month.

> **You may not need any of this.** If you already have ABCD minimally
> preprocessed data, or can get it by another route, the pipeline does not
> require Globus — it requires objects at known S3 keys. See
> [data-ingress.md](data-ingress.md).

---

## Summary

| Gate | What | Who grants it | Typical time | Can it be automated? | Gate id |
|---|---|---|---|---|---|
| 1 | ABCD data access (NDA Data Use Certification) | NIMH Data Archive + your institution's signing official | Weeks | No | `nda_duc` |
| 2 | Globus subscription, **High Assurance** tier | Globus, via your institution | Days–weeks (or already exists) | No | `globus_subscription` |
| 3 | Your endpoint added to that subscription | A human Globus admin at your institution | Hours–days | No | `endpoint_subscription` |
| 4 | The GCS build itself | You | ~1 day | Mostly — `pixi run globus` does it; registering two Globus apps and one login are in a browser | `globus_app_registration`, `globus_login` |

The **gate id** is how `pixi run globus setup-status` names the gate when a step is
waiting on it, and `--json` output carries the same id, so this page and the tool
refer to each gate the same way. The full list, with the text the tool shows for
each, is in `src/globus_admin/gates.py`.

Gates 1 and 2 are independent — start both at once. Gate 3 requires gate 2 and an
endpoint UUID from the first half of gate 4. Gate 4's storage-gateway step cannot
complete until gate 3 is done.

```
Gate 1 (NDA DUC) ─────────────────────────┐
                                          ├──> transfers work
Gate 2 (HA subscription) ──> Gate 3 ──────┤
                              ↑           │
Gate 4a (endpoint setup) ─────┘           │
                                          │
Gate 4b (gateway, collection, key pair, token) ────┘
```

**Nothing on this page asks anyone for an AWS credential**, and that is recent.
Until 2026-09 an IAM user with a long-lived access key was required here, and in
accounts that forbid creating IAM users it was the longest-lead item in the whole
setup. It is gone: the storage gateway now writes to S3 through a signing proxy on
the instance's own loopback interface, which signs with a role the instance assumes
([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md)). Terraform and the
machine image build both halves. You still type a key pair at one prompt in gate 4,
but it is a placeholder that authenticates nothing — see
[the key pair is not a secret](#the-key-pair-you-register-is-not-a-secret).

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
subscription includes HA but not the S3 connector, you are not blocked, but the
route has changed: the POSIX+EBS staging path was removed in 2026-09
([ADR 001](decisions/001-s3-gateway-over-posix-staging.md)). Stage the data into
the bucket by whatever means you have and submit with `ingress-mode=presynced`
([data-ingress.md](data-ingress.md)) — that needs no S3 add-on and no static IAM
user key, and the `ingress-verify` step checks the layout before anything
consumes it.

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
failure appears much later — creating the High Assurance storage gateway fails
with a subscription error at
[globus-setup.md step 3.6](globus-setup.md#36-create-the-storage-gateway-and-collection). This is
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
exist until you have run the first half of gate 4 (`globus bootstrap-endpoint`).
So the build order is: create the endpoint → request the subscription → then
continue with the storage gateway.

**How to obtain it.**

1. Run [globus-setup.md steps 3.1–3.4](globus-setup.md#3-the-setup-path) to
   create the endpoint. `bootstrap-endpoint` ends by printing the request text,
   with the endpoint UUID in it.
2. Send that text to your institution's Globus administrators
   ([globus-setup.md step 3.5](globus-setup.md#35-ask-for-the-subscription)).
   If you have lost it, `pixi run globus setup-status` attaches it to the
   `endpoint_subscription` gate.
3. Wait for confirmation before running `globus configure`.

**How to verify you cleared it.** `pixi run globus doctor`'s subscription check
passes, and `setup-status` shows the subscription step as done.

**What failure looks like.** Gateway creation fails with a subscription error,
as in gate 2.

> **The subscription is tied to the endpoint UUID, not the EC2 instance.** This
> matters operationally: routine instance replacement keeps the UUID (the new
> instance re-registers its node against the existing endpoint on boot), so you
> do not re-request. But if the *endpoint* is ever deleted and recreated, you
> must go back to your Globus admins with the new UUID — and register a new
> service client first, because Globus will not let the one that created an
> endpoint create another. See
> [globus.md → Endpoint recovery](globus.md#endpoint-recovery-after-accidental-deletion).

---

## No longer required: an IAM user, or any AWS access key

**What you need in AWS is permission to run this repository's Terraform** — nothing
else, and nobody else's approval. There is no IAM user to request, no access key to
be issued, and no key to rotate later.

This is worth stating as its own section because it used to be a gate, and one that
stopped people. The old requirement was an IAM **user** with an access key, because
the Globus S3 connector authenticates with a key pair and supports no role or
instance profile. Many institutional AWS accounts sit under an AWS Organizations
*service control policy* that denies `iam:CreateUser` outright — an SCP deny cannot
be overridden by any role in the account, `AdministratorAccess` included — so for
those accounts the requirement was not a step but a request to another team, with a
lead time measured in weeks. **This deployment's account is one of them.**

What replaced it, in one paragraph: the gateway is pointed at a small HTTPS proxy
listening on the instance's own `127.0.0.1`. The proxy throws away whatever
credentials arrive, re-signs each request with credentials the instance obtains from
its own IAM role, and forwards it to S3. The role is confined to the one bucket
prefix that gateway writes to, and its credentials expire hourly. Nothing long-lived
exists to leak, and nothing has to be requested. The reasoning, the alternatives, and
the measured cost of putting a proxy in the path are in
[ADR 019](decisions/019-signing-proxy-for-globus-ingress.md).

Two consequences for you:

- **Terraform creates the roles; the machine image carries the proxy.** Both happen
  in [globus-setup.md step 3.2](globus-setup.md#32-create-the-aws-infrastructure).
  There is no separate step and no waiting.
- **Your own AWS access is still SSO.** `pixi run globus` refuses to run under
  anything but an IAM Identity Center profile, static keys included, so "no access
  keys" applies to operators as well as to the gateway.

### The key pair you register is not a secret

Globus Connect Server will not create an S3 storage gateway without a key pair, so
one is still typed at a prompt in gate 4. **It is a placeholder, and it must not be
treated as a credential.** The proxy discards the signature made with it without
checking it; AWS never sees it. This deployment's staging gateway holds AWS's own
published example key, `AKIAIOSFODNN7EXAMPLE`, precisely so that nobody mistakes it
for a live one — and if it leaked, it would give the finder exactly what it gives
Globus: nothing.

This matters because the instinct to protect a "credential" is the one thing that
can break the design. What keeps the proxy safe is that it is reachable **only from
the instance itself**. Anyone who "hardens" the setup by putting the key pair in a
secrets manager and letting a remote client reach the listener has removed the
control and kept the decoration. There is nothing to steal here; there is only a
socket to keep local.

If you want to check it rather than take this on trust, try the key against AWS. It
comes back `InvalidAccessKeyId`, not `AccessDenied` — AWS resolves the key id before
it ever looks at a signature, so the pair does not name a principal at all:

```bash
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE \
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
AWS_EC2_METADATA_DISABLED=true \
  aws s3api list-objects-v2 --bucket <your-bucket> --max-items 1
```

---

## Gate 4 — The build itself

With gates 1–3 cleared, [globus-setup.md](globus-setup.md) is the ordered
procedure. This section covers only what that guide assumes you already
decided.

### The two Globus registrations

Two Globus registrations are involved (gate id `globus_app_registration` for both),
and confusing them is the most common setup error. They are not interchangeable.
Only the second has to be made in a browser. The third row is not a registration at
all, and is listed here only because it is typed at a prompt in the same sitting:

| # | Registration | Type | Created at | Used for | Answers field |
|---|---|---|---|---|---|
| 1 | Service client, e.g. `cloudpipe-gcs` | Service account / **confidential client**, with a client secret | `pixi run globus register-service-client`, or [app.globus.org/settings/developers](https://app.globus.org/settings/developers) | Creates and owns the endpoint, and runs every non-interactive `globus-connect-server` command after that | `service_client_id`, `service_client_secret_ref` |
| 2 | Native app, e.g. `cloudpipe-transfer` | **Native app** | [app.globus.org/settings/developers](https://app.globus.org/settings/developers), and only there | `pixi run globus login`, and the Argo transfer step's refresh token | `native_app_client_id` |
| 3 | The gateway's key pair | Neither — [a placeholder, not a credential](#the-key-pair-you-register-is-not-a-secret) | Nowhere; you invent it, or use AWS's published example key | Satisfies the connector's demand for a key pair. The signing proxy discards it | none — typed at a prompt, stored only by Globus |

Registration 2 stays a browser job because a native app is what a *person* logs in
through, and a browser flow cannot be scripted. Registration 1 is four Globus Auth
API calls, so `register-service-client` makes it, stores the secret Globus
discloses once, and never shows it to anyone.

The fourth call is the one that is easy to miss, and it is not optional: the client
must be an **administrator of its Auth project**. `globus-connect-server endpoint
setup` refuses to run as an identity that administers no project, and a client that
merely belongs to one does not qualify — passing `--project-id` fails too, with
*"does not exist or … is not an admin on the project and can not view it"*. The
Developers Portal does not add the client as an administrator, so a registration
made there needs `pixi run globus grant-project-admin --project-id <uuid>`
afterwards. Note what this privilege is: a project administrator may create and
delete clients in that project, so the endpoint's own credential can do so. That is
a reason to give a deployment's client a project of its own.

Using registration 1's UUID where registration 2 is expected fails with *"Only
native app clients can access this URL"* — a confusing error whose cause is
simply the wrong app type.

The service client is **used up by the endpoint it creates**: Globus will not let
the same client create a second endpoint. Recreating an endpoint therefore starts
with registering a new service client — `globus delete-service-client --client-id
<recorded id>`, then `globus register-service-client`.

**One browser flow is irreducible.** `pixi run globus login` (gate id
`globus_login`) needs an interactive login, with `prompt=login` forced because
reusing an existing browser session can produce a `No effective ACL rules` 403
on an HA collection. It is a Globus security control that exists deliberately,
and it recurs every session lifetime. Do not build automation around it. (The
endpoint used to need two more; `bootstrap-endpoint` creates it as the service
client instead.)

### Why the gateway still asks for a key pair at all

The GCS S3 connector has **no IMDS / EC2 instance role support**, and its
credential schema has no field for a session token, so it cannot hold temporary AWS
credentials either. It supports exactly three credential modes, and only one of them
works for an automated pipeline: `--s3-user-credential` combined with
`--admin-managed-credentials`, where the admin registers one key pair that applies to
all mapped identities.

So a key pair has to exist. What changed is what it *is*: the gateway's S3 endpoint
points at the signing proxy on loopback rather than at AWS, the proxy replaces the
signature with one made from the instance role's temporary credentials, and the
registered pair therefore authenticates nothing
([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md), and
[not a secret](#the-key-pair-you-register-is-not-a-secret) above).

AWS Mountpoint for S3 would have let the connector use the instance role directly
and is **not** an alternative: it supports only sequential writes from byte 0, while
GridFTP's Extended Block Mode writes out of order, producing
`500 globus_xio: System error in write: Invalid argument`.

### Decisions to make before you start

Each is a field in the answers document
([globus-setup.md step 3.1](globus-setup.md#31-fill-in-the-answers-document)),
which `globus init` turns into the Terraform variables.

| Decision | Answers field | Guidance |
|---|---|---|
| Globus ingress, or stage the data yourself? | none — `ingress-mode` is per workflow | `globus` needs the S3 connector in your subscription. Without it, use `presynced` — there is no longer a POSIX staging variant |
| Which identity domain may authenticate? | `identity_domain` | Your institution's domain. Passed as `--domain` at gateway creation |
| Who owns the endpoint? | `owner_email` | An institutional Globus admin, not a personal account — personal ownership breaks when the person leaves |
| Source collection and path | `source_collection_id`, `source_base_path` | From gate 1 |
| Which SSH sources are allowed? | `admin_prefix_list_id` | An AWS managed prefix list. Ports 443 and 50000–51000 **must** be open to `0.0.0.0/0` — GridFTP data channels are peer-to-peer worldwide and access control is enforced by Globus OAuth2, not by network filtering |

---

## The ongoing manual burden

Setup is one-time; these are not. Anyone evaluating whether to reproduce this
ingress should weigh the recurring cost, because it is the part that actually
hurts.

| Task | Frequency | Automatable? | Symptom when missed |
|---|---|---|---|
| Re-authenticate the HA session with `pixi run globus login` | **Every 7 days today** — governed by our own gateway's `--authentication-timeout-mins` (`10080`). This is *not* a Globus limit: the ABCD source collection is High Assurance at `525600` (1 year), so there is no 30-day HA ceiling. See [ADR 010's Correction](decisions/010-globus-ha-subscription.md). | **No** — HA requires an interactive `prompt=login` | Presents as `not_from_allowed_domain` — *not* as an expiry message |
| ~~Rotate the S3 gateway IAM access key~~ | **Never — there is no key to rotate.** The signing proxy's role credentials expire hourly and are re-obtained by the proxy ([ADR 019](decisions/019-signing-proxy-for-globus-ingress.md)) | n/a — the task is gone, not automated | n/a |
| Re-request subscription after endpoint deletion | Only after endpoint loss | No | Gateway operations fail with a subscription error |

The weekly session lapse is worth calling out separately: it does **not**
announce itself as an expiry. It surfaces as a domain-restriction error, which
sends people looking at `--domain` and identity mapping instead of at token
age. Check token age first.

The Globus refresh token's rotation procedure is in
[globus.md → Credential rotation](globus.md#credential-rotation). The S3 key
rotation that page used to describe is gone with the key.

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
- [ADR 019](decisions/019-signing-proxy-for-globus-ingress.md) — why no IAM user is needed, and why the key pair you register is not a secret

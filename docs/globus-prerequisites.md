# Globus session — prerequisites

[globus.md](globus.md) tells you how to log in and keep the session alive. This
page tells you what you must already have before that login can work, and **most
of it cannot be obtained from this repository, by Terraform, or by any amount of
automation.** It is an approval granted by another organisation, on its timeline,
plus one registration and one login that only a person in a browser can make.

> **You may not need any of this.** If you already have ABCD minimally
> preprocessed data, or can get it by another route, the pipeline does not
> require Globus — it requires objects at known S3 keys. See
> [data-ingress.md](data-ingress.md#pre-staging-without-globus-ingress-modepresynced).

---

## Summary

| # | What | Who grants it | Typical time | Can it be automated? | Gate id |
|---|---|---|---|---|---|
| 1 | ABCD data access (NBDC Data Use Certification), on a Globus identity | NBDC Data Hub + your institution's signing official | Weeks | No | `nda_duc` |
| 2 | A Globus native app registration | You, in the Globus Developers Portal | Minutes | No — a browser only | `globus_app_registration` |
| 3 | The login itself, every 30 days | You, in a browser | Minutes | No | `globus_login` |

The **gate id** is the `x-gate` value the answers schema
(`src/globus_admin/schemas/answers.schema.json`) attaches to each field a gate
produces, and `globus_login` is also the `gate` a CLI error carries when a login is
what is missing ([globus-contract.md](globus-contract.md)).

**What you do not need.** No Globus subscription, High Assurance or otherwise; no
Globus endpoint, collection or storage gateway of your own; no service client; no
machine image; no IAM user or AWS access key. Each of those belonged to the Globus
Connect Server endpoint this deployment ran until 2026-11-12 (ADR 024). A Globus
*transfer* into an endpoint of your own required that endpoint to be High
Assurance as well (ADR 010); reading the source collection's own HTTPS server does
not. In AWS you need permission to apply this repository's Terraform, and an SSO
login ([globus.md → Before the first login](globus.md#before-the-first-login)).

---

## 1 — ABCD data access

**What it is.** ABCD data is distributed by the [NIH Brain Development Cohorts
(NBDC) Data Hub](https://www.nbdc-datahub.org), which replaced the NIMH Data
Archive (NDA) as ABCD's distributor. Access is granted under an NBDC **Data Use
Certification (DUC)**, applied for as an individual or a group. The minimally
preprocessed imaging data that CloudPipe consumes (Hagler et al. 2019) is
prepared by the ABCD **DAIRC** and served from a Globus collection on the NBDC
Data Hub.

**Why it gates everything.** The NBDC Data Hub collection is a Globus High
Assurance collection precisely *because* it holds DUC-governed human subjects
data. No DUC, no access to the collection, whichever way you read it.

**How to obtain it.** The NBDC's [data access
process](https://www.nbdc-datahub.org/data-access-process) is authoritative; in
outline:

1. Sign in to the NBDC Data Hub through RAS with your institutional email
   address.
2. Identify your institution's **signing official** — the person authorised to
   sign data use agreements on the institution's behalf. This is usually in the
   sponsored programs or research compliance office, not in your lab or
   department.
3. Confirm your institution has an active **Federalwide Assurance (FWA)**. The
   NBDC requires one; an institution without it cannot sign.
4. Submit the DUC. It includes a research data use statement, a plan for
   deleting the data when the project ends, and an attestation of compliance
   with **NIST SP 800-171**. You sign it through DocuSign, then the signing
   official countersigns.
5. The NBDC Data Access Committee reviews the request.
6. Once approved, complete the NBDC Responsible Data and Biospecimen Use
   Training (a score of at least 90% on its quiz is required).
7. Ask the NBDC how they grant Globus access to the imaging collection for your
   approved identity.

**How to verify you cleared it.** Log into [app.globus.org](https://app.globus.org)
with the identity your DUC is associated with, search the collection list for the
NBDC Data Hub's ABCD collection by name (it is listed as "NBDC Datashare ABCD
Release"), and browse it. If you can list a subject directory, this gate is clear.
Three values come out of it:

| Value | Where it goes |
|---|---|
| The collection's UUID, from its overview page | `globus_source_collection_id` in your tfvars, and `source_collection_id` in the answers document |
| The path you browsed to, the parent of the subject directories | `globus_source_base_path` in your tfvars, and `source_base_path` in the answers document |
| The domain of the identity you logged in with, for example your university's | `identity_domain` in the answers document |

The identity domain matters because the collection's High Assurance policy
accepts sessions only from the domains it names. `login` asks Globus for a session
in that domain and refuses to store a login made as any other identity.

**What failure looks like.** The collection is not searchable, or browsing it
returns a permissions error rather than a listing. Both mean the access grant
has not propagated to the identity you are logged in as. Check that you are
logged in with the *same* identity your DUC names — a personal Globus account
and an institutional one are different identities even with the same email.

**Timeline.** Weeks is normal. The institutional signature and the Data Access
Committee review are usually the long poles, and neither is something you can
expedite from your side.

> The NBDC Data Hub owns this process and changes it independently of CloudPipe.
> Their documentation is authoritative; treat the steps above as orientation,
> not as a specification.

---

## 2 — The native app registration

**What it is.** A Globus **native app** is what a person logs in through. `pixi run
globus login` runs its browser flow, and the refresh tokens it produces are the
ones the ingress lists and copies with. A native app has no secret.

**How to obtain it.** At
[app.globus.org/settings/developers](https://app.globus.org/settings/developers),
register a new app of type **native app**, for example `cloudpipe-transfer`, that
may request refresh tokens. Copy its client UUID into the answers document as
`native_app_client_id`.

Register a native app, not a confidential client: a confidential client's UUID
where a native app is expected fails with *"Only native app clients can access this
URL"*. A native app that may not request refresh tokens fails the login with
`globus.no_refresh_token`, and nothing is stored.

After the first login the client id is stored beside the tokens, so a later login
needs neither the answers field nor `--client-id`.

---

## 3 — The login, every 30 days

**One browser flow is irreducible.** `pixi run globus login` forces a fresh
authentication (`prompt=login`) in the source's identity domain, because reusing
an existing browser session can produce a session the High Assurance policy does
not accept. It is a Globus security control that exists deliberately. Do not build
automation around it.

**It recurs.** This deployment holds a login to **30 days**: the ingress pod and
the queue manager's batch gate both refuse an older one. The 30 days is this
deployment's own rule; the source collection itself allows a year. The renewal
procedure, and how a lapsed session presents, are in
[globus.md → Session renewal](globus.md#session-renewal).

| Task | Frequency | Automatable? | Symptom when missed |
|---|---|---|---|
| Renew the session with `pixi run globus login` | every 30 days, at most | **No** | the batch gate refuses the batch; a hand-submitted `https-transfer` exits 78 naming the login |

---

## Decision: should you reproduce this?

Answer these in order. The first "no" is your answer.

1. **Do you have, or can you obtain, an approved NBDC DUC for ABCD?**
   No → you cannot use this data at all. Nothing else matters.
2. **Are you pulling directly from the NBDC Data Hub Globus collection?**
   No → use pre-staged S3 ingress ([data-ingress.md](data-ingress.md)). Skip
   Globus entirely.
3. **Can someone with that access log in in a browser at least every 30 days?**
   No → pre-stage in bulk instead of running the HTTPS ingress continuously.

If all three are yes, [globus.md](globus.md) will work for you.

---

## Related

- [data-ingress.md](data-ingress.md) — the S3 contract the ingress satisfies, and the alternatives
- [globus.md](globus.md) — the runbook: login, doctor, renewal, troubleshooting
- [globus-contract.md](globus-contract.md) — the CLI's machine-readable outputs
- ADR 024 — why the Globus host was retired and HTTPS is the only network ingress

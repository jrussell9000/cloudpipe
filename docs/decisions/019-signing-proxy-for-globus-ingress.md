# 019 — A loopback signing proxy in the Globus ingress path, so no long-lived AWS key exists

**Status**: Accepted (staging proven live 2026-09-24; the production gateway still
authenticates with its IAM user's key until the cutover in
`openspec/changes/retire-globus-s3-access-keys` section 8)

## Context

Globus Connect Server's S3 connector authenticates to S3 with an access key pair
and nothing else. Its credential schema says so explicitly, in every published
version:

```json
{ "DATA_TYPE": "s3_user_credential_policies#1.2.0",
  "s3_key_id": "string",
  "s3_secret_key": "string" }    // no session token, in any version
```

Until this decision, that pair was a real AWS IAM user's access key, and two costs
followed from it:

- **A long-lived credential that works from anywhere on the internet.** It is held
  by Globus, not by AWS or by the instance, and rotating it is a hand procedure.
  On this deployment the user holds its maximum of two keys, so a
  create-then-delete rotation has no free slot at all (POA&M P3-10). The key's
  policy also grants `s3:PutObject` on `arn:aws:s3:::<YOUR_S3_BUCKET>/*` **and `*`** — every
  bucket in the account.
- **A dependency on someone else's approval.** An AWS Organizations service control
  policy denies `iam:CreateUser` in this account, and an SCP deny cannot be
  overridden by any role in it. A new adopter of this repository could not create
  the user themselves, which broke the goal that setup need no outside approval.

Two facts shaped the answer. The GCS instance **already has an IAM role** with an
instance profile, so a process on it can obtain AWS credentials with no stored
secret. And staging and production **share one instance and one endpoint**, so
whatever replaces the key has to keep those two environments apart on one host.

The full design, with every alternative weighed, is
`openspec/changes/retire-globus-s3-access-keys/design.md` (D1–D8, R1–R2).

## Decision

### 1. A proxy, because the schema leaves no alternative

The simpler design is a **credential refresher**: a daemon that assumes a role on
a timer and re-registers the resulting credentials through the GCS Manager API,
putting nothing in the data path. **It cannot work.** STS credentials are invalid
without their session token, and the credential schema above has no field to carry
one — verified against the published schema, not assumed.

Since Globus will accept only a static pair, the only way to stop that pair from
being an AWS credential is to make it authenticate to something that is not AWS.
That is a proxy by definition. This is not a preference among options; it is the
only remaining shape.

### 2. Envoy, not VersityGW or s3proxy

| | Frontend credential concept | Backend auth without a static AWS key |
|---|---|---|
| **Envoy** | none — signs whatever arrives | **yes** — instance profile / STS AssumeRole |
| VersityGW | yes — real IAM service, policies | **no** — static `--access`/`--secret` |
| s3proxy | thin | unverified |

VersityGW is the better-engineered *gateway* — Go, single binary, a genuine IAM
layer with bucket policies, commercially maintained — and it is the wrong tool
here for one decisive reason: its S3 backend takes static credentials, and its own
documentation states that multi-user setups use "a single configured backend access
account for all backend access". It would need the IAM user this decision exists to
remove, and two gateways would need two of them. s3proxy's backend credential
handling was never verified, because Envoy's was already confirmed; that is a gap
in the comparison and not a finding against s3proxy.

Envoy's gap is that it does **not** verify the inbound signature. That is addressed
by items 3 and 4 below rather than by code.

### 3. One listener and one assumed role per gateway

Not one shared proxy. Each storage gateway gets its own loopback listener and its
own prefix-scoped role, reached by `sts:AssumeRole` from the instance role:

```
staging gateway    → 127.0.0.1:8443 → cloudpipe-globus-staging-writer    (scratch/globus-staging/*)
production gateway → 127.0.0.1:8444 → cloudpipe-globus-production-writer (mmps_mproc/*)
```

This is what keeps the proxy **out of the security path**. Confinement is an IAM
property of each role, enforced by AWS and provable read-only with
`iam:SimulatePrincipalPolicy` — which is what `globus doctor` check 12 calls. A
proxy that enforces nothing cannot enforce it wrongly. The roles' sessions last one
hour and are deliberately not raised: the point of the change is that the
credential in the ingress path expires, and Envoy re-obtains it for as long as the
listener runs.

The rejected alternative is one proxy holding per-credential prefix rules, which
would move the staging/production boundary into proxy configuration — the same
objection that made pointing staging at production's IAM user unacceptable.

### 4. The key pair Globus holds is not a secret, and reachability is the control

Envoy discards the inbound signature without checking it, so the key registered
into the gateway authenticates nothing. The value registered on the staging gateway
is AWS's own published example key, `AKIAIOSFODNN7EXAMPLE`. That is safe only
because of two other properties, which are therefore load-bearing:

- **The listener binds `127.0.0.1` and nothing else.** Anything that can reach it
  is already executing on the GCS host.
- **The role behind it is prefix-scoped**, so the worst a local caller can do is
  what that gateway is already permitted to do.

Compared with the static key this replaces, it is a net reduction in exposure: that
key works from any internet host for whoever obtains it, is unrotatable today, and
reaches every bucket in the account.

The practical consequence is a documentation rule with teeth: **never describe the
registered pair as a credential.** Someone who believes it is one will eventually
try to "protect" it — and the obvious way to do that, binding the listener to an
interface where a secrets manager or a remote client can reach it, inverts the
security model. There is nothing to steal; there is only a socket to keep local.

For the same reason the listener has **no Envoy admin interface**. An earlier draft
reserved ports 9443/9444 for one. The admin interface is unauthenticated and serves
`/quitquitquit` and `/config_dump`, so anything that can reach the listener could
use it, and reserving ports nothing binds would only send a later reader looking for
a process that is not there.

### 5. TLS on loopback

Every documented `--s3-endpoint` example is HTTPS, including Globus's own
ActiveScale example `https://localhost:8443` — which usefully confirms that a
loopback endpoint is a supported pattern — and no TLS-verification escape hatch is
documented for the connector. So the listener terminates TLS with a certificate
generated at AMI bake time and trusted by the host.

That turns the open question into "whose trust store does the connector read", which
is tractable. Answered by inspection: the data path is native C over
libcurl/OpenSSL, so `update-ca-certificates` on the AMI is sufficient. The
management plane is a separate Python client with its own certifi bundle and never
talks to S3 at all, so it needs nothing.

### 6. Signing runs as an upstream filter

SigV4 covers the `Host` header, and the proxy rewrites `Host` to the real S3
endpoint — so signing must happen *after* that rewrite or every request fails with
`SignatureDoesNotMatch`. Envoy expresses this as an upstream HTTP filter on the
cluster, terminated by `upstream_codec`. `use_unsigned_payload: true` stops Envoy
buffering request bodies to hash them, which is what makes multi-gigabyte uploads
viable at all.

## Consequences

**Setup no longer waits on anyone.** No IAM user, no access key, and nothing to
request from an AWS organization administrator. The roles and the listeners are
Terraform and the AMI.

**A process now sits in the data path that did not before.** Measured on the live
staging gateway, 2026-09-24, on the `c5n.xlarge` GCS instance:

| Measurement | Direct to S3 | Through the listener |
|---|---|---|
| 2 GiB synthetic, from tmpfs, 8 MB parts | 358.6 MiB/s | 277.4 MiB/s (**77.4%**) |
| Real subject, 130 files, 14.92 GiB | not measured | **134.0 MiB/s** |

The 22.6% regression is charged against headroom rather than against transfer time:
real ingress runs at 134 MiB/s, bounded by the wide-area path from the source
collection, which is 2.1x below the proxy's ceiling. Envoy cost 0.52 of a core
during that transfer (3.94 CPU-seconds per GiB) and peaked at 58.8 MiB of memory.
Sizing anything on this should use 3.94 CPU-s/GiB, not the 2.47 the single-stream
synthetic run showed — more files means more connections and more signatures per
byte.

**One interop limit, for clients other than Globus.** Envoy's signer cannot hash a
streaming body, so a client that sends a flexible checksum as a *trailer* — AWS CLI
v2's default — fails with `InvalidArgument: aws-chunked encoding is not supported
when x-amz-content-sha256 UNSIGNED-PAYLOAD is supplied`. GridFTP is unaffected. Any
other client of the listener needs
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required`.

**`doctor` reports the new credential at the old check id.** Check 12
(`doctor.s3_gateway_credential`) keeps its id and position and changes its question
from "is an IAM user named and does it hold a usable key" to "is this gateway's role
assumable, correctly scoped, and is its listener answering". An appended check 14
(`doctor.s3_listener`) covers listener liveness, so "the role is fine but the proxy
is down" does not present as a credential problem. Both report `skipped` for a
gateway not yet cut over from a static key.

**Loopback-only binding is verified, not assumed** — `ss -lntp` shows the sockets on
`127.0.0.1` only; a connection to the instance's own private address on the port is
refused **from the host itself**, where no security group is in the path; and an
off-host connection times out. The middle test is the load-bearing one, since the
off-host result would look identical if Envoy bound `0.0.0.0`.

**It withdraws, rather than satisfies, an earlier requirement.** "S3 gateway
credentials come from Secrets Manager", with its two key-rotation scenarios, existed
to make a long-lived key safe to hold. This removes the key instead, so that
requirement and its scenarios were withdrawn in `simplify-globus-ingress`' own
delta.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Credential refresher, no proxy | STS credentials need a session token; the connector's credential schema has no field for one |
| VersityGW | Its S3 backend needs static credentials — the IAM user this removes |
| s3proxy | Unverified backend credential handling; Envoy's was already confirmed |
| One shared proxy with per-credential prefix rules | Moves the staging/production boundary into proxy configuration instead of IAM |
| Plain HTTP on loopback | No documented TLS escape hatch for the connector; the trust store question turned out to be tractable |
| Widening the listener's binding to protect the key pair | Inverts the model — the pair is not a secret and the binding is the control |
| AWS Mountpoint for S3 (and so the instance role) | Sequential writes from byte 0 only; GridFTP's Extended Block Mode writes out of order and fails with `500 globus_xio: System error in write: Invalid argument` ([ADR 001](001-s3-gateway-over-posix-staging.md)) |

## Related

- [ADR 001](001-s3-gateway-over-posix-staging.md) — why the S3 gateway, not POSIX+EBS staging
- [ADR 010](010-globus-ha-subscription.md) — why High Assurance is mandatory
- [ADR 018](018-declarative-gcs-config-and-operator-cli.md) — the declared configuration this listener is published in
- [globus-setup.md](../globus-setup.md) — the build procedure, including the key pair that is not a secret
- [globus-prerequisites.md](../globus-prerequisites.md) — what setup still needs from other people
- [globus-contract.md](../globus-contract.md) — `doctor` checks 12 and 14

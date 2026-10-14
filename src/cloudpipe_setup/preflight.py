"""Read-only verification of what the answers assert about the world.

Collection succeeds or fails on the deployer's answers alone. Preflight succeeds
or fails on the world, which changes underneath them — a hosted zone moves
accounts, a prefix list is deleted, an SSO session expires. That is why this is a
separate command rather than the last phase of `setup` (design D7).

Two rules hold over every check here, and both have teeth:

**Nothing is created, modified or deleted.** Every outward call goes through
`World`, which exposes reads and nothing else, and `tests/test_setup_wizard_preflight.py`
asserts on the recorded call list rather than on an absence of errors — a run that
mutated something successfully also raises nothing.

**A check that could not be performed reports `skipped`, never `pass`.** Being
told a deployment is ready by a check that lacked the permission to look is worse
than being told nothing, because the deployer acts on it. Every `skipped` names
the action that was missing.

The Cloudflare API token is read from `CLOUDFLARE_API_TOKEN` and from nowhere
else. It is never prompted for, never written, never printed, and never placed in
a process argument list — which is why the HTTP calls here use `urllib` with an
`Authorization` header rather than shelling out to `curl`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .exits import CliError, ExitCode, Remedy
from .output import Record

#: Stable check identifiers. These are a published contract — a consumer keys its
#: display off them — so they are never renamed, and a new check is a new entry.
CHECK_IDENTITY = "aws.identity"
CHECK_ACCOUNT = "aws.account"
CHECK_HOSTED_ZONE = "aws.hosted_zone"
CHECK_PREFIX_LISTS = "aws.prefix_lists"
CHECK_DATA_BUCKET = "aws.data_bucket"
CHECK_CONFIG_OBJECTS = "aws.config_objects"
CHECK_AMIS = "aws.amis"
CHECK_IMAGE_SCANNING = "aws.image_scanning"
CHECK_CLOUDFLARE = "cloudflare.token"
CHECK_CLOUDFLARE_ORGANIZATION = "cloudflare.organization"
CHECK_FEDERATION_METADATA = "federation.metadata"
CHECK_FEDERATION_EVIDENCE = "federation.mfa_evidence"
CHECK_FEDERATION_SECRET = "federation.client_secret"
CHECK_GLOBUS_INPUTS = "globus.inputs"

# Contract 2.0 removed two checks whose subjects the answers no longer held
# (design D7 of openspec/changes/optional-domain-and-cognito-auth):
# `oidc.discovery`, of the institution's issuer, which a Cognito deployment does
# not have, and `aws.access_oidc_secret`, of a hand-created Access client
# secret, which Cognito replaces with one Terraform creates. The three
# `federation.*` checks above are their successors, under the optional
# `cognito_federation` input: `federation.metadata` fetches an OIDC issuer's
# discovery document or a SAML provider's metadata, and `federation.secret`
# looks for the OIDC client secret the deployer creates by hand. Each reports
# `skipped` for a deployment that federates nothing, which is most of them.

ORDER = (
    CHECK_IDENTITY,
    CHECK_ACCOUNT,
    CHECK_HOSTED_ZONE,
    CHECK_PREFIX_LISTS,
    CHECK_DATA_BUCKET,
    CHECK_CONFIG_OBJECTS,
    CHECK_AMIS,
    CHECK_IMAGE_SCANNING,
    CHECK_CLOUDFLARE,
    CHECK_CLOUDFLARE_ORGANIZATION,
    CHECK_FEDERATION_METADATA,
    CHECK_FEDERATION_EVIDENCE,
    CHECK_FEDERATION_SECRET,
    CHECK_GLOBUS_INPUTS,
)
"""The order checks run and are reported in: identity first, because every AWS
check after it is `skipped` without one."""

TOKEN_ENV = "CLOUDFLARE_API_TOKEN"


def _cli(subcommand: str, *, task: str = "cloudpipe") -> str:
    from .render import cli_command

    return cli_command(subcommand, task=task)


CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

HTTP_TIMEOUT = 15

#: Error codes that mean "you were not allowed to look", across the four services
#: called here — they each spell it differently. Anything in this set becomes
#: `skipped` with the action named; anything else is a real answer.
_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AuthFailure",
        "Forbidden",
        "NotAuthorizedException",
        "UnrecognizedClientException",
    }
)

#: Codes that mean the credentials are gone or stale rather than insufficient.
#: Separated because the remedy differs: one is `aws sso login`, the other is a
#: conversation with whoever owns the permission set.
_EXPIRED_CODES = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "RequestExpired",
    }
)


class World:
    """Every outward read preflight makes, and nothing else.

    A seam, deliberately narrow. It exists so a test can record the exact calls a
    run issues: asserting "no mutation happened" by observing that nothing broke
    would also hold for a run that mutated something and succeeded.

    boto3 is imported lazily. Preflight on a root with no Globus inputs rendered
    should report that in milliseconds, and a deployer running `--help` should not
    pay for an SDK import.

    `region` is the deployment's, from the answers. It is passed to every client
    rather than left to the AWS CLI profile, because two checks read regional
    resources — a managed prefix list and a Secrets Manager secret each exist in
    one region only. Left to the
    profile, a deployer whose CLI default differs
    from the deployment is told an id that exists "does not resolve". It is set on
    the client, not the session, so it applies to an injected session too and a
    test can see it.
    """

    def __init__(
        self, *, session: Any = None, fetch: Any = None, region: str | None = None
    ) -> None:
        self._explicit_session = session
        self._fetch = fetch
        self._session: Any = None
        self._region = region or None

    @property
    def session(self) -> Any:
        if self._explicit_session is not None:
            return self._explicit_session
        if self._session is None:
            import boto3

            self._session = boto3.Session()
        return self._session

    @property
    def region(self) -> str:
        """The region the regional reads target, as a message should name it."""
        return (
            self._region
            or getattr(self.session, "region_name", None)
            or "the AWS profile's default region"
        )

    def _client(self, service: str) -> Any:
        # `region_name=None` is boto3's own default, so an unset region falls back
        # to the profile exactly as a bare `client(service)` would.
        return self.session.client(service, region_name=self._region)

    def credential_method(self) -> str:
        """How the resolved credentials were obtained, or "" when there are none.

        `get_frozen_credentials()` is what actually loads and validates an SSO
        token, so this is also where an expired session surfaces.
        """
        credentials = self.session.get_credentials()
        if credentials is None:
            return ""
        credentials.get_frozen_credentials()
        return str(getattr(credentials, "method", "") or "")

    def caller_identity(self) -> dict[str, Any]:
        return dict(self._client("sts").get_caller_identity())

    def hosted_zones(self, domain: str) -> list[dict[str, Any]]:
        """Hosted zones whose name matches `domain`, exactly as the stack looks it up.

        `terraform/modules/stack/dns.tf:1` is `data "aws_route53_zone" { name =
        var.domain }`, which matches the zone name and nothing else — so a check
        that accepted a parent zone would pass for a deployment whose `plan` then
        fails on that data source.
        """
        client = self._client("route53")
        response = client.list_hosted_zones_by_name(DNSName=domain, MaxItems="5")
        wanted = domain.rstrip(".") + "."
        return [zone for zone in response.get("HostedZones", ()) if zone.get("Name") == wanted]

    def managed_prefix_list(self, identifier: str) -> dict[str, Any] | None:
        client = self._client("ec2")
        response = client.describe_managed_prefix_lists(PrefixListIds=[identifier])
        found = response.get("PrefixLists", ())
        return dict(found[0]) if found else None

    def bucket_region(self, name: str) -> str:
        """The bucket's region, which doubles as the existence test.

        `GetBucketLocation`, not `HeadBucket`. A HEAD response carries no body, so
        botocore can only report the status line — a missing bucket and a bucket in
        someone else's account both arrive as an opaque `404`/`403` with no error
        code to read, and `_error_code` below would classify neither. This returns
        a real `NoSuchBucket` or `AccessDenied`.

        `""` is what AWS returns for us-east-1, historically; it is passed through
        rather than rewritten, because the caller reports it alongside the
        deployment's own region and "" is not a region a deployer should be told
        theirs matches.
        """
        response = self._client("s3").get_bucket_location(Bucket=name)
        return str(response.get("LocationConstraint") or "us-east-1")

    def images(self, **query: Any) -> list[dict[str, Any]]:
        """`DescribeImages` — the images visible to these credentials.

        An AMI id that is neither owned by nor shared with this account is not
        visible, and that is precisely the question: EC2 fails the launch with
        `InvalidAMIID.NotFound` for the same reason.
        """
        response = self._client("ec2").describe_images(**query)
        return [dict(image) for image in response.get("Images", ())]

    def object_keys(self, bucket: str, prefix: str) -> list[tuple[str, int]]:
        """(key, size) of up to 20 objects under `prefix`. `ListObjectsV2`, never a GET.

        Not `HeadObject`: a HEAD response has no body, so a missing key and a
        denied read both arrive as a bare status with no error code to tell them
        apart. Only keys and sizes are returned; no object is read.
        """
        response = self._client("s3").list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
        return [
            (str(item["Key"]), int(item.get("Size", 0))) for item in response.get("Contents", ())
        ]

    def inspector_ecr_status(self) -> str:
        """Amazon Inspector's ECR scanning status for this account, in this region.

        `BatchGetAccountStatus` with no account ids answers for the caller. Inspector
        is regional, which is why this goes to the deployment's region like the
        other regional reads.
        """
        response = self._client("inspector2").batch_get_account_status()
        accounts = response.get("accounts") or [{}]
        return str(accounts[0].get("resourceState", {}).get("ecr", {}).get("status") or "")

    def describe_secret(self, name: str) -> dict[str, Any]:
        """`DescribeSecret`, never `GetSecretValue`.

        The check is that the secret exists. Reading it would put an identity
        provider's client secret in this process for no benefit, and the
        permission to describe is the one a deployer is likelier to hold.
        """
        return dict(self._client("secretsmanager").describe_secret(SecretId=name))

    def fetch_json(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """A GET, returning (status, parsed body). Never a POST, PUT or DELETE.

        `urllib` rather than a subprocess: a token in an `Authorization` header is
        invisible to `ps`, and a token in a `curl` argument list is not.
        """
        if self._fetch is not None:
            return self._fetch(url, headers or {})

        request = urllib.request.Request(url, headers=headers or {}, method="GET")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read().decode("utf-8"))
            except (ValueError, OSError):
                return err.code, None


def run(
    answers: dict[str, Any],
    root: Path,
    *,
    world: World | None = None,
    expected_account: str | None = None,
    repo: Path | None = None,
) -> list[Record]:
    """Every check, in `ORDER`, as one `Record` each.

    The identity is resolved first and threaded through, because an AWS check with
    no credentials must report `skipped` rather than fail with a stack trace — and
    because non-SSO credentials stop the run outright, before anything is read.
    """
    world = world or World(region=str(answers.get("region") or "") or None)
    repo = repo or Path(__file__).resolve().parents[2]

    identity_record, identity = _check_identity(world)
    token_record = _check_cloudflare(world, answers)
    records = [
        identity_record,
        _check_account(identity, expected_account),
        _check_hosted_zone(world, answers, identity),
        _check_prefix_lists(world, answers, root, identity),
        _check_data_bucket(world, root, identity),
        _check_config_objects(world, root, repo, identity),
        _check_amis(world, root, repo, identity),
        _check_image_scanning(world, identity),
        token_record,
        _check_cloudflare_organization(world, answers, token_record),
        _check_federation_metadata(world, answers),
        _check_federation_evidence(answers),
        _check_federation_secret(world, answers, identity),
        _check_globus_inputs(root, repo),
    ]
    assert [record.identifier for record in records] == list(ORDER)
    return records


def exit_code(records: list[Record]) -> ExitCode:
    """`3` when any check failed, `0` when every check passed or was skipped.

    A `skipped` run is not a green run and the human output says so, but it is not
    a failure either: the deployer may legitimately not hold a read permission the
    apply does not need from them.
    """
    return ExitCode.CHECK_FAILED if any(r.state == "fail" for r in records) else ExitCode.OK


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def _check_identity(world: World) -> tuple[Record, dict[str, Any] | None]:
    """The resolved AWS identity, and that it came from an SSO profile.

    Non-SSO credentials raise rather than returning a record: the spec requires
    exit `4` "without acting", and continuing to read the world with static keys
    would be acting. This mirrors `globus_admin/prereqs.py:159` exactly, including
    the strictness — `method == "sso"` and nothing else — so a deployer who reads
    the rule from either tool reads the same rule.
    """
    try:
        method = world.credential_method()
    except Exception as err:  # noqa: BLE001 - botocore raises a wide family here
        return _credentials_unavailable(err), None

    if not method:
        return (
            Record(
                identifier=CHECK_IDENTITY,
                title="AWS identity",
                state="fail",
                message="no AWS credentials could be resolved from this environment.",
                remedy=Remedy("command", "aws sso login"),
            ),
            None,
        )

    if method != "sso":
        raise CliError(
            "aws.not_sso",
            f"these credentials resolved from `{method}`, not from an AWS SSO profile. Preflight "
            "requires SSO: static access keys are not supported, and this tool never takes one as "
            "an argument or writes one to a file.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("command", "aws configure sso && aws sso login"),
            detail={"credential_method": method},
        )

    try:
        identity = world.caller_identity()
    except Exception as err:  # noqa: BLE001
        return _credentials_unavailable(err), None

    return (
        Record(
            identifier=CHECK_IDENTITY,
            title="AWS identity",
            state="pass",
            message=f"SSO credentials for account {identity.get('Account', 'unknown')} "
            f"({identity.get('Arn', 'no arn')}).",
        ),
        identity,
    )


def _credentials_unavailable(err: Exception) -> Record:
    expired = _error_code(err) in _EXPIRED_CODES or "expired" in str(err).lower()
    return Record(
        identifier=CHECK_IDENTITY,
        title="AWS identity",
        state="fail",
        message=(
            "the AWS SSO session has expired." if expired else "AWS credentials did not resolve."
        )
        + f" {err}",
        remedy=Remedy("command", "aws sso login"),
    )


def _check_account(identity: dict[str, Any] | None, expected: str | None) -> Record:
    """The resolved account against the one the deployer intended.

    Skipped when no intended account was given, because there is nothing to
    compare and a comparison against nothing is not a pass. The wrong-account case
    this guards is quiet and expensive: every resource applies cleanly, into an
    account nobody meant to use.
    """
    if identity is None:
        return _skipped(
            CHECK_ACCOUNT,
            "Intended account",
            "no identity resolved, so there is no account to compare.",
            Remedy("command", "aws sso login"),
        )
    resolved = str(identity.get("Account", ""))
    if not expected:
        return _skipped(
            CHECK_ACCOUNT,
            "Intended account",
            f"credentials resolve to account {resolved}, and no intended account was given to "
            "check it against.",
            Remedy(
                "command",
                f"cloudpipe preflight --account {resolved} (if that is the one you "
                "mean to deploy into)",
            ),
        )
    if resolved != expected:
        return Record(
            identifier=CHECK_ACCOUNT,
            title="Intended account",
            state="fail",
            message=f"these credentials are for account {resolved}, not the intended "
            f"{expected}. Every resource would apply cleanly into the wrong account.",
            remedy=Remedy("command", "aws sso login --profile <the profile for that account>"),
        )
    return Record(
        identifier=CHECK_ACCOUNT,
        title="Intended account",
        state="pass",
        message=f"account {resolved}, as intended.",
    )


def _check_hosted_zone(
    world: World, answers: dict[str, Any], identity: dict[str, Any] | None
) -> Record:
    domain = answers.get("domain")
    if "domain" in answers and domain is None:
        # Chosen, not missing: port-forward mode needs no zone. Reported as
        # skipped because nothing was checked — never as pass, and never as the
        # failure an absent zone would be with a domain.
        return _skipped(
            CHECK_HOSTED_ZONE,
            "Route53 hosted zone",
            "domain is null (port-forward mode), so no hosted zone is needed and none was "
            "looked for.",
            Remedy(
                "human",
                f"Nothing to fix. Reach the web UIs with `{_cli('ui <name>')}`; set a "
                "domain only if you want them published under hostnames.",
            ),
        )
    if not domain:
        return _skipped(
            CHECK_HOSTED_ZONE,
            "Route53 hosted zone",
            "no domain is set in the answers, so there is no zone to look for.",
            Remedy("command", "cloudpipe setup"),
        )
    if identity is None:
        return _no_credentials(CHECK_HOSTED_ZONE, "Route53 hosted zone")

    try:
        zones = world.hosted_zones(str(domain))
    except Exception as err:  # noqa: BLE001
        return _translate(
            err, CHECK_HOSTED_ZONE, "Route53 hosted zone", "route53:ListHostedZonesByName"
        )

    if not zones:
        return Record(
            identifier=CHECK_HOSTED_ZONE,
            title="Route53 hosted zone",
            state="fail",
            message=f"no hosted zone named {domain} exists in account "
            f"{identity.get('Account', 'unknown')}. ACM certificate validation is DNS-based, so it "
            "would fail against a zone this account does not own, and the apply would wait on a "
            "record it cannot write.",
            remedy=Remedy(
                "human",
                f"Create or transfer the hosted zone for {domain} into this account, or set "
                "`domain` to one it does own.",
            ),
        )
    return Record(
        identifier=CHECK_HOSTED_ZONE,
        title="Route53 hosted zone",
        state="pass",
        message=f"{domain} is hosted here ({zones[0].get('Id', 'no id')}).",
    )


def _check_prefix_lists(
    world: World, answers: dict[str, Any], root: Path, identity: dict[str, Any] | None
) -> Record:
    """The managed prefix list the deployment references: `globus init`'s.

    Read out of the rendered tfvars rather than the answers document — it is not
    the wizard's field to own (design D4). The wizard's own prefix-list field left
    with contract 2.0, because nothing read it; the check keeps its identifier,
    and a dict, so a second id is one more entry rather than a new check.
    """
    from . import roots

    if not roots.globus_ingress_enabled(root):
        # The only prefix list the stack references is the Globus host's SSH
        # rule, inside `module.globus`. With the ingress off that module has no
        # instances, so there is no rule to resolve an id for — and reporting
        # this as outstanding would send a deployer to look up an id nothing
        # reads. See the spec: a check whose subject is not configured is not run.
        return _skipped(
            CHECK_PREFIX_LISTS,
            "Managed prefix lists",
            "globus_enabled is not true in this root, so the stack creates no security-group "
            "rule that references a managed prefix list.",
            Remedy("human", "Nothing to do unless you enable the Globus ingress."),
        )

    wanted = {}
    globus_id = _globus_prefix_list_id(root)
    if globus_id:
        wanted["globus_admin_prefix_list_id"] = globus_id

    if not wanted:
        return _skipped(
            CHECK_PREFIX_LISTS,
            "Managed prefix lists",
            "globus_admin_prefix_list_id is not set in any tfvars file in this root.",
            Remedy("command", _cli("init", task="globus")),
        )
    if identity is None:
        return _no_credentials(CHECK_PREFIX_LISTS, "Managed prefix lists")

    missing, resolved = [], []
    for variable, identifier in sorted(wanted.items()):
        try:
            found = world.managed_prefix_list(identifier)
        except Exception as err:  # noqa: BLE001
            if _error_code(err) in _DENIED_CODES:
                return _translate(
                    err,
                    CHECK_PREFIX_LISTS,
                    "Managed prefix lists",
                    "ec2:DescribeManagedPrefixLists",
                )
            found = None
        if found is None:
            missing.append(f"{variable}={identifier}")
        else:
            resolved.append(f"{variable}={identifier} ({found.get('PrefixListName', 'unnamed')})")

    if missing:
        return Record(
            identifier=CHECK_PREFIX_LISTS,
            title="Managed prefix lists",
            state="fail",
            message=f"{', '.join(missing)} does not resolve in account "
            f"{identity.get('Account', 'unknown')}, region {world.region}. The ingress and SSH "
            "security-group rules reference it directly, so the apply fails on the rule rather "
            "than on the id.",
            remedy=Remedy(
                "human",
                "Check the id in the AWS console under VPC > Managed prefix lists, in the same "
                "region as the deployment. A prefix list is regional.",
            ),
        )
    return Record(
        identifier=CHECK_PREFIX_LISTS,
        title="Managed prefix lists",
        state="pass",
        message="; ".join(resolved),
    )


def _check_data_bucket(world: World, root: Path, identity: dict[str, Any] | None) -> Record:
    """The data bucket the stack configures but never creates.

    `terraform/modules/stack/logging.tf` says it outright — "the bucket predates
    Terraform" — and four resources then act on it by name: its access logging,
    its lifecycle rules, and the Argo and Prefect IAM policies that scope to its
    ARN. On a fresh account the bucket is not there and those fail with
    `NoSuchBucket`, in the untargeted apply, after the cluster exists. That is the
    most expensive moment in the install to discover a bucket name.

    Read from the rendered tfvars, not the answers document: like the prefix list,
    `globus_s3_destination_bucket` is `globus init`'s field and not the wizard's
    (design D4).
    """
    from . import roots

    value, _ = roots.tfvars_assignments(root).get(roots.DATA_BUCKET_VARIABLE, (None, None))
    name = value if isinstance(value, str) and value else None
    if not name:
        kind, text = roots.missing_globus_remedy(root, (roots.DATA_BUCKET_VARIABLE,))
        return _skipped(
            CHECK_DATA_BUCKET,
            "Data bucket",
            f"{roots.DATA_BUCKET_VARIABLE} is not set in any tfvars file in this root.",
            Remedy(kind, text),
        )
    if identity is None:
        return _no_credentials(CHECK_DATA_BUCKET, "Data bucket")

    try:
        region = world.bucket_region(name)
    except Exception as err:  # noqa: BLE001
        code = _error_code(err)
        if code in _DENIED_CODES:
            # Not `_translate`, which the other denied reads use, because S3's
            # namespace is global and this one answer has two causes: the
            # credentials lack the permission, or the name belongs to another
            # account entirely. A deployer who chose a short, generic bucket name
            # hits the second, and "ask for s3:GetBucketLocation" would send them
            # to the wrong person.
            return _skipped(
                CHECK_DATA_BUCKET,
                "Data bucket",
                f"s3://{name} could not be read ({code}). Either these credentials lack "
                "s3:GetBucketLocation, or the name belongs to another AWS account — bucket "
                "names are global, and both answer the same way.",
                Remedy(
                    "human",
                    "Ask whoever owns your permission set for s3:GetBucketLocation. If they "
                    f"confirm you have it, the name {name} is taken: choose another and set "
                    "globus_s3_destination_bucket to it.",
                ),
            )
        return Record(
            identifier=CHECK_DATA_BUCKET,
            title="Data bucket",
            state="fail",
            message=f"s3://{name} does not exist in account "
            f"{identity.get('Account', 'unknown')} ({code or 'no error code'}). The stack "
            "configures this bucket by name — access logging, lifecycle rules, and the Argo and "
            "Prefect policies that scope to its ARN — and never creates it, so the apply fails "
            "on those resources rather than on the name.",
            # Not `aws s3api create-bucket`, which this used to print: a bucket made
            # that way has no transport-encryption policy and no `prevent_destroy`,
            # which is the posture the bootstrap root exists to give it.
            remedy=Remedy(
                "human",
                f"Apply the bootstrap root with data_bucket={name} — step 4 of "
                f"docs/deployer-first-hour.md, and `{_cli('setup')}` prints the command. "
                "If it is already applied under another name, set "
                f"{roots.DATA_BUCKET_VARIABLE} to that name instead.",
            ),
        )

    message = f"s3://{name} exists, in {region}"
    if world.region not in ("the AWS profile's default region", region):
        # Not a failure: the stack applies against a bucket in another region. It
        # is said out loud because every transfer into it then crosses a region
        # boundary, and nothing later in the install mentions it again.
        message += f", which is not this deployment's region ({world.region})"
    return Record(identifier=CHECK_DATA_BUCKET, title="Data bucket", state="pass", message=message)


def _check_config_objects(
    world: World, root: Path, repo: Path, identity: dict[str, Any] | None
) -> Record:
    """Every `config/` object the workflows stage is in the data bucket.

    A new bucket has none, and the stack creates none. Without them every workflow
    fails at artifact staging, the first step of each pod, long after the install
    reported success. The keys are derived from the templates and the code they
    run (`config_objects.required_keys`), so a template that starts staging a new
    object is checked without this function changing.

    Presence only, by listing. No object is read, so the FreeSurfer license's
    contents never reach this process.
    """
    from . import config_objects, roots

    title = "Reference files"
    value, _ = roots.tfvars_assignments(root).get(roots.DATA_BUCKET_VARIABLE, (None, None))
    bucket = value if isinstance(value, str) and value else None
    if not bucket:
        return _skipped(
            CHECK_CONFIG_OBJECTS,
            title,
            f"{roots.DATA_BUCKET_VARIABLE} is not set, so there is no bucket to look in.",
            Remedy("human", f"Set {roots.DATA_BUCKET_VARIABLE}; aws.data_bucket says how."),
        )
    if identity is None:
        return _no_credentials(CHECK_CONFIG_OBJECTS, title)

    required = config_objects.required_keys(repo)
    if not required:
        return _skipped(
            CHECK_CONFIG_OBJECTS,
            title,
            f"no config/ key could be read from the workflow templates under {repo}. Run this from "
            "a clone of the repository.",
            Remedy("human", "Check that the clone's argo/workflows/ is intact."),
        )

    missing: list[str] = []
    try:
        for key in required:
            present = world.object_keys(bucket, key)
            exact = any(name == key for name, _ in present)
            # A directory artifact (config/fsLR) is present when it holds a real file,
            # not only the zero-byte "folder" marker the console creates.
            directory = any(name.startswith(key + "/") and size > 0 for name, size in present)
            if not (exact or directory):
                missing.append(key)
    except Exception as err:  # noqa: BLE001
        code = _error_code(err)
        if code in _DENIED_CODES:
            return _translate(err, CHECK_CONFIG_OBJECTS, title, "s3:ListBucket")
        if code == "NoSuchBucket":
            return _skipped(
                CHECK_CONFIG_OBJECTS,
                title,
                f"s3://{bucket} does not exist, so nothing in it could be checked; see aws.data_bucket.",
                Remedy("human", "Create the bucket first (aws.data_bucket's remedy)."),
            )
        raise

    if not missing:
        return Record(
            identifier=CHECK_CONFIG_OBJECTS,
            title=title,
            state="pass",
            message=f"all {len(required)} config/ objects the workflows stage are in s3://{bucket}.",
        )

    by_provider: dict[str, list[str]] = {}
    remedies: list[str] = []
    for key in missing:
        provider = config_objects.provider(key)
        label = provider.name if provider else "nothing known"
        by_provider.setdefault(label, []).append(key)
        text = (
            provider.remedy if provider else f"{key} has no known source; read {required[key][0]}."
        )
        if text not in remedies:
            remedies.append(text)
    listed = "; ".join(f"{', '.join(keys)} ({label})" for label, keys in by_provider.items())
    return Record(
        identifier=CHECK_CONFIG_OBJECTS,
        title=title,
        state="fail",
        message=f"s3://{bucket} is missing {_count(len(missing), len(required))} config/ objects the "
        f"workflows stage: {listed}. "
        "The install would succeed and every workflow would then fail at artifact staging.",
        remedy=Remedy("human", " ".join(remedies)),
    )


def _globus_prefix_list_id(root: Path) -> str | None:
    """`globus_admin_prefix_list_id` as Terraform will see it, from whichever file wins.

    Rendered by `globus init` or written by hand into `terraform.tfvars`: reading
    only the rendered file skipped this id for every root that sets it by hand.
    """
    from . import roots

    value, _ = roots.tfvars_assignments(root).get("globus_admin_prefix_list_id", (None, None))
    return value if isinstance(value, str) and value else None


def _verify_cloudflare_token(
    world: World, headers: dict[str, str], account: str | None
) -> tuple[int, Any]:
    """(status, body) of the first verify endpoint that accepts the token, else the last.

    A user token answers `/user/tokens/verify`; an account-owned token answers
    only `/accounts/<id>/tokens/verify`. Both are valid for Terraform, so both
    are tried — the second only when an account id is known to build it from.
    """
    urls = [f"{CLOUDFLARE_API}/user/tokens/verify"]
    if account:
        urls.append(f"{CLOUDFLARE_API}/accounts/{account}/tokens/verify")
    status, body = 0, None
    for url in urls:
        status, body = world.fetch_json(url, headers=headers)
        if status == 200 and isinstance(body, dict) and body.get("success"):
            break
    return status, body


def _probe_cloudflare_permissions(
    world: World, headers: dict[str, str], account: str
) -> tuple[list[Any], list[Any], list[str], list[Any]]:
    """(granted, refused, unclear, disabled) across `cloudflare.PERMISSIONS`, one read each.

    The status alone is not the answer. Every Cloudflare v4 response carries a
    `success` flag, and a 200 whose body says `success: false` is a refusal —
    judging by status alone would report that as a grant, which is the one
    outcome this check exists to rule out.

    Nor is every refusal about the token. `disabled` holds the groups Cloudflare
    answered with `access.api.error.not_enabled`: Zero Trust is off in the
    account, and whether the token holds the group cannot be known until it is on.
    """
    from . import cloudflare

    granted, refused, unclear, disabled = [], [], [], []
    for permission in cloudflare.PERMISSIONS:
        status, body = world.fetch_json(
            f"{CLOUDFLARE_API}/accounts/{account}{permission.probe}", headers=headers
        )
        succeeded = body.get("success") if isinstance(body, dict) else None
        if status in (401, 403) or (status == 200 and succeeded is False):
            # Cloudflare's own reason travels with the refusal, so the deployer
            # sees why and not only that.
            reason = _cloudflare_errors(body)
            if cloudflare.ACCESS_NOT_ENABLED in reason:
                disabled.append(permission)
                continue
            refused.append((permission, f"HTTP {status}{': ' + reason if reason else ''}"))
        elif status == 200 and succeeded is True:
            granted.append(permission)
        else:
            unclear.append(f"{permission.name} (HTTP {status})")
    return granted, refused, unclear, disabled


def _check_cloudflare(world: World, answers: dict[str, Any]) -> Record:
    """The token is present, valid, active, and can reach every group the stack uses.

    Four permission groups, one read-only probe each (`cloudflare.PERMISSIONS`).
    This used to probe one, `access/identity_providers`, so a token holding that
    group alone passed and then failed at phase 4 on the tunnel or the device
    settings — after the cluster existed.

    The probes prove READ access, and that is the most a read-only check can
    prove: Cloudflare offers no call that reports a token's own permissions
    without "API Tokens: Read", which a deployment token should not carry. The
    gap is closed from the other side — the link this check prints selects Write
    for every group, so a token made from it has the level the apply needs.

    Two failures look identical at Cloudflare's end and need different fixes, so
    they are told apart here. A token that verifies but is refused by EVERY probe
    is almost always scoped to a different account (a second deployment's token
    exported in the first one's shell is the case that prompted this); a token
    refused by SOME probes is missing those groups.

    Both token kinds verify. A user token answers `/user/tokens/verify`; an
    account-owned token answers only `/accounts/<id>/tokens/verify`, so the second
    is tried when the first refuses and an account id is known.

    The token value appears in no message, no detail and no argument list. Only
    the header it is sent in, which is why this goes through `urllib`.
    """
    from . import cloudflare

    title = "Cloudflare API token"
    account = answers.get("cloudflare_account_id")
    link = cloudflare.token_template_url(account)
    create = Remedy(
        "human",
        f"Create a token from this link, which selects every permission the stack needs"
        f"{f' and scopes it to account {account}' if account else ''}: {link} — then "
        f"`export {TOKEN_ENV}=<the token>` in the shell you install from. By hand, the rows "
        f"are: {cloudflare.permission_rows()}.",
    )

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            f"{TOKEN_ENV} is not set in this environment, so there is no token to verify. It is "
            "read from the environment only: this tool never prompts for it, stores it or prints "
            "it.",
            create,
        )

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    unreachable = Remedy("human", "Try again from a network that can reach api.cloudflare.com.")

    try:
        status, body = _verify_cloudflare_token(world, headers, account)
    except (urllib.error.URLError, OSError, ValueError) as err:
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            f"the Cloudflare API could not be reached ({err}).",
            unreachable,
        )

    if status in (400, 401, 403) or (isinstance(body, dict) and body.get("success") is False):
        return Record(
            identifier=CHECK_CLOUDFLARE,
            title=title,
            state="fail",
            message=f"Cloudflare rejected the token in {TOKEN_ENV} (HTTP {status}"
            f"{': ' + _cloudflare_errors(body) if _cloudflare_errors(body) else ''}). It is not "
            "a valid token — mistyped, revoked, or the wrong variable exported.",
            remedy=create,
        )
    if status != 200 or not isinstance(body, dict) or not body.get("success"):
        return Record(
            identifier=CHECK_CLOUDFLARE,
            title=title,
            state="fail",
            message=f"token verification returned HTTP {status} without a success body.",
            remedy=Remedy("human", f"Check {TOKEN_ENV} and the Cloudflare API's status page."),
        )

    state = str((body.get("result") or {}).get("status", "unknown"))
    if state != "active":
        return Record(
            identifier=CHECK_CLOUDFLARE,
            title=title,
            state="fail",
            message=f"the token in {TOKEN_ENV} is {state}, not active.",
            remedy=create,
        )

    if not account:
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            "the token is valid and active, and cloudflare_account_id is not set, so what it can "
            "reach could not be checked. A valid token with the wrong scope fails at apply.",
            Remedy("command", _cli("setup")),
        )

    try:
        granted, refused, unclear, disabled = _probe_cloudflare_permissions(world, headers, account)
    except (urllib.error.URLError, OSError, ValueError) as err:
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            f"the token is valid and active, but what it can reach could not be checked ({err}).",
            unreachable,
        )

    if refused and not granted:
        return Record(
            identifier=CHECK_CLOUDFLARE,
            title=title,
            state="fail",
            message=f"the token is valid, but every one of the {len(refused)} probes in account "
            f"{account} was refused. A token that verifies and can see nothing in the account is "
            "almost always scoped to a DIFFERENT account — check that the token you exported is "
            "this deployment's, not another one's.",
            remedy=create,
        )
    if refused:
        names = ", ".join(f"{p.name} ({p.dashboard_level})" for p, _ in refused)
        why = "; ".join(f"{p.name}: {reason}" for p, reason in refused)
        return Record(
            identifier=CHECK_CLOUDFLARE,
            title=title,
            state="fail",
            message=f"the token is valid and reaches account {account}, but is missing "
            f"{len(refused)} of the {len(cloudflare.PERMISSIONS)} permission groups the stack "
            f"needs: {names}. The phase 4 apply would fail on "
            f"{'; '.join(p.needed_by for p, _ in refused)}. Cloudflare said: {why}.",
            remedy=Remedy(
                "human",
                f"Edit the token and add {names}, scope Account, for account {account} — or "
                f"replace it with one made from this link: {link}. Permission changes take "
                "several minutes to reach every endpoint, so re-run this after a short wait "
                "rather than editing again.",
            ),
        )
    if unclear:
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            f"the token is valid and active; {', '.join(unclear)} returned neither a grant nor a "
            "refusal.",
            Remedy("human", "Check the Cloudflare API's status page and run this again."),
        )
    if disabled:
        # Not a fail: nothing is wrong with the token that is known. The fix is in
        # the account, and `cloudflare.organization` reports it as a failure there.
        names = ", ".join(p.name for p in disabled)
        return _skipped(
            CHECK_CLOUDFLARE,
            title,
            f"the token is valid and can read {len(granted)} of the {len(cloudflare.PERMISSIONS)} "
            f"permission groups in account {account}. The other {len(disabled)} ({names}) cannot "
            "be checked yet: Cloudflare answers them with \"Access is not enabled\", because Zero "
            "Trust is not turned on in the account. Re-run this once it is.",
            Remedy("human", cloudflare.ENABLE_ZERO_TRUST),
        )
    return Record(
        identifier=CHECK_CLOUDFLARE,
        title=title,
        state="pass",
        message=f"the token in {TOKEN_ENV} is active and can read all "
        f"{len(cloudflare.PERMISSIONS)} permission groups the stack uses in account {account}. "
        "Read is what can be checked without changing anything; a token made from the link "
        "this tool prints has Write.",
    )


def _check_cloudflare_organization(
    world: World, answers: dict[str, Any], token_record: Record
) -> Record:
    """Zero Trust is on in the account, and its team domain is the one in the answers.

    Two failures this catches before the cluster exists rather than at phase 4.
    Zero Trust not turned on: the provider's create is a PUT on the existing
    organization and cannot make one, and the dashboard is the only way to make
    one, because onboarding asks for a plan and payment details even on Free
    (confirmed in the second-account rerun, 2026-10-13). And a team domain that
    differs from the account's: the apply would send it as `auth_domain`, trying
    to rename the domain every enrolled WARP client and the identity provider's
    redirect URI use.

    Runs only after `cloudflare.token` passed, or skipped because Zero Trust is
    off — the one case the token check cannot judge and this check exists for.
    Its probes include a read in the same permission group as this one, so a
    refusal here is about the organization and not the token, and a bad token is
    reported once, there.
    """
    from . import cloudflare

    title = "Zero Trust organization"
    account = answers.get("cloudflare_account_id")
    expected = str(answers.get("cloudflare_team_domain") or "").strip().rstrip(".").lower()
    enable = Remedy("human", cloudflare.ENABLE_ZERO_TRUST)

    zero_trust_off = token_record.remedy is not None and token_record.remedy == enable
    if token_record.state != "pass" and not zero_trust_off:
        return _skipped(
            CHECK_CLOUDFLARE_ORGANIZATION,
            title,
            f"{CHECK_CLOUDFLARE} did not pass, so there is no token this can read the "
            "organization with.",
            token_record.remedy or Remedy("human", f"Resolve {CHECK_CLOUDFLARE} first."),
        )

    token = os.environ.get(TOKEN_ENV, "").strip()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        status, body = world.fetch_json(
            f"{CLOUDFLARE_API}/accounts/{account}/access/organizations", headers=headers
        )
    except (urllib.error.URLError, OSError, ValueError) as err:
        return _skipped(
            CHECK_CLOUDFLARE_ORGANIZATION,
            title,
            f"the Cloudflare API could not be reached ({err}).",
            Remedy("human", "Try again from a network that can reach api.cloudflare.com."),
        )

    succeeded = isinstance(body, dict) and body.get("success") is True
    result = body.get("result") if succeeded else None
    found = str((result if isinstance(result, dict) else {}).get("auth_domain") or "")
    found = found.strip().rstrip(".").lower()
    if status != 200 or not found:
        reason = _cloudflare_errors(body)
        if cloudflare.ACCESS_NOT_ENABLED in reason:
            return Record(
                identifier=CHECK_CLOUDFLARE_ORGANIZATION,
                title=title,
                state="fail",
                message=f"Zero Trust is not turned on in account {account}: Cloudflare answers "
                f'"{reason}". Terraform cannot turn it on: it updates the organization and '
                "cannot create one, and onboarding asks for a plan and payment details.",
                remedy=enable,
            )
        return Record(
            identifier=CHECK_CLOUDFLARE_ORGANIZATION,
            title=title,
            state="fail",
            message=f"account {account} has no Zero Trust organization that this token can read "
            f"(HTTP {status}{': ' + reason if reason else ''}). Zero Trust is most likely not "
            "turned on yet. Terraform cannot turn it on: it updates the organization and "
            "cannot create one.",
            remedy=enable,
        )

    if found != expected:
        return Record(
            identifier=CHECK_CLOUDFLARE_ORGANIZATION,
            title=title,
            state="fail",
            message=f"account {account}'s team domain is {found}, but cloudflare_team_domain is "
            f"{expected or 'not set'}. The apply sends cloudflare_team_domain as the "
            "organization's auth_domain, so it would try to rename the team domain that every "
            "enrolled WARP client and the identity provider's redirect URI use.",
            remedy=Remedy(
                "command",
                f"{_cli('setup')}   (and give {found} as the team domain; rename it in the "
                "dashboard first only if you mean to)",
            ),
        )

    return Record(
        identifier=CHECK_CLOUDFLARE_ORGANIZATION,
        title=title,
        state="pass",
        message=f"Zero Trust is on in account {account}, and its team domain is {found}, as "
        "the answers say.",
    )


#: Where the stack names the machine images it launches. Read, never restated:
#: a check that kept its own copy of an AMI id would pass against an id the stack
#: no longer uses.
_GLOBUS_CALL = "terraform/modules/stack/globus.tf"
_KARPENTER_CALL = "terraform/modules/stack/karpenter.tf"
_STACK_VARIABLES = "terraform/modules/stack/variables.tf"
_GPU_AMI_DOC = "docs/pre-baked-amis.md"


def _check_amis(world: World, root: Path, repo: Path, identity: dict[str, Any] | None) -> Record:
    """Whether the machine images the stack launches exist in this account.

    Two images, with different consequences, so they are judged differently:

    * **The Globus host's AMI**, a literal id in `globus.tf`, launched only with
      `globus_enabled`. Not visible means the phase 4 apply fails with
      `InvalidAMIID.NotFound` after the cluster exists — which is how the second
      deployment found that the id was this project's own image, shared with
      nobody. `fail`.
    * **The GPU node AMI**, chosen by Terraform (`terraform/modules/gpu-ami-choice`):
      this account's bake, else the publisher's pinned public AMI, else the stock
      AL2023 AMI. All three launch GPU nodes, so all three pass, with a note
      saying which. It used to be `blocked` when no bake matched, because the node
      class then selected nothing; the stock fallback ended that. The one `fail`
      is a pinned public AMI this account cannot see (`_gpu_ami_choice`).

    `terraform plan` would catch neither. An AMI is only resolved at apply, and a
    tag selector only by Karpenter at runtime.
    """
    from . import roots

    title = "Machine images"
    if identity is None:
        return _no_credentials(CHECK_AMIS, title)

    sources = _ami_sources(root, repo)
    if sources is None:
        return _skipped(
            CHECK_AMIS,
            title,
            f"the image references could not be read from {repo / _GLOBUS_CALL} and "
            f"{repo / _KARPENTER_CALL}. Run this from a clone of the repository.",
            Remedy("human", "Check that the clone's terraform/modules/stack/ is intact."),
        )
    globus_ami, gpu_tags = sources
    account = identity.get("Account", "unknown")

    try:
        globus_missing = False
        if roots.globus_ingress_enabled(root):
            try:
                globus_missing = not world.images(ImageIds=[globus_ami])
            except Exception as err:  # noqa: BLE001
                if _error_code(err) not in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
                    raise
                globus_missing = True
        gpu_found = world.images(
            Owners=["self"],
            Filters=[{"Name": f"tag:{key}", "Values": [value]} for key, value in gpu_tags.items()],
        )
    except Exception as err:  # noqa: BLE001
        if _error_code(err) in _DENIED_CODES:
            return _translate(err, CHECK_AMIS, title, "ec2:DescribeImages")
        raise

    try:
        gpu_failure, gpu_note = _gpu_ami_choice(world, root, repo, gpu_tags, gpu_found, account)
    except Exception as err:  # noqa: BLE001
        if _error_code(err) in _DENIED_CODES:
            return _translate(err, CHECK_AMIS, title, "ec2:DescribeImages")
        raise

    if globus_missing:
        return Record(
            identifier=CHECK_AMIS,
            title=title,
            state="fail",
            message=f"globus_enabled is true, and the Globus host's AMI {globus_ami} is not "
            f"visible from account {account} — neither owned by it nor shared with it. The phase "
            f"4 apply would fail with InvalidAMIID.NotFound, after the cluster exists. {gpu_note}",
            remedy=Remedy(
                "human",
                "Build the Globus Connect Server image in this account from packer/globus-gcs/, "
                f"and set globus_ami_id in {_GLOBUS_CALL} to its id — or leave globus_enabled "
                "unset; the pipeline runs without the ingress.",
            ),
        )
    if gpu_failure is not None:
        return gpu_failure
    globus = (
        f"Globus host AMI {globus_ami}: visible. "
        if roots.globus_ingress_enabled(root)
        else "Globus host AMI: not needed, globus_enabled is not true. "
    )
    return Record(identifier=CHECK_AMIS, title=title, state="pass", message=f"{globus}{gpu_note}")


_PUBLISHED_GPU_AMIS = "terraform/modules/karpenter/published-gpu-amis.json"


def _gpu_ami_choice(
    world: World,
    root: Path,
    repo: Path,
    gpu_tags: dict[str, str],
    gpu_found: list[dict[str, Any]],
    account: str,
) -> tuple[Record | None, str]:
    """(a failure, or None; a note) for the AMI the GPU node class will be rendered with.

    The same choice `terraform/modules/gpu-ami-choice` makes, in the same order:
    this account's bake by tag, else the publisher's AMI pinned for this region,
    else the stock AL2023 alias. Only the middle one can fail. Terraform renders a
    pinned id without looking it up, because the only owner it could name is the
    publisher's account, which the public tree does not carry. So a pinned AMI
    this account cannot see would be launched by nothing, silently, and it is
    caught here instead.
    """
    from . import roots

    if gpu_found:
        return (
            None,
            f"GPU node AMI: this account's bake ({gpu_found[0].get('ImageId', 'unknown id')}).",
        )

    selection, _ = roots.tfvars_assignments(root).get("gpu_ami_selection", ("auto", None))
    stock = (
        "GPU node AMI: the stock EKS-optimized AL2023 AMI (gpu_ami_alias). GPU nodes pull the "
        "FastSurfer and FireANTs images at start, which adds minutes to each new node's first step."
    )
    if selection == "alias":
        return None, stock + " Chosen by gpu_ami_selection = \"alias\"."

    try:
        published = json.loads((repo / _PUBLISHED_GPU_AMIS).read_text())
    except (OSError, ValueError):
        published = {}
    pinned = None
    if (
        published.get("fastsurfer_digest") == gpu_tags["fastsurfer-image-digest"]
        and published.get("fireants_digest") == gpu_tags["fireants-image-digest"]
        and published.get("eks_version") == gpu_tags["eks-version"]
    ):
        pinned = (published.get("amis") or {}).get(world.region)
    if not pinned:
        return None, stock + " No pre-baked AMI is published for these images in this region."

    try:
        visible = bool(world.images(ImageIds=[pinned]))
    except Exception as err:  # noqa: BLE001
        if _error_code(err) not in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
            raise
        visible = False
    if visible:
        return (
            None,
            f"GPU node AMI: the publisher's pre-baked AMI {pinned}, pinned in {_PUBLISHED_GPU_AMIS}.",
        )
    return (
        Record(
            identifier=CHECK_AMIS,
            title="Machine images",
            state="fail",
            message=f"The GPU node class would be rendered with the published AMI {pinned}, pinned "
            f"for {world.region} in {_PUBLISHED_GPU_AMIS}, and account {account} cannot see it. "
            "Karpenter would launch no GPU node, and nothing would say why.",
            remedy=Remedy(
                "human",
                'Set gpu_ami_selection = "alias" in terraform.tfvars to use the stock AMI, or pull a '
                "newer commit of this repository if the publisher has replaced that AMI.",
            ),
        ),
        "",
    )


def _check_image_scanning(world: World, identity: dict[str, Any] | None) -> Record:
    """Whether Amazon Inspector scans this account's container images.

    The stack configures ENHANCED registry scanning, including the pull-through
    cache's repositories, but ENHANCED delegates to Inspector, and Inspector is
    enabled per account and region outside the stack. With it off the
    configuration applies cleanly and nothing is ever scanned: no error, and no
    vulnerability evidence for NIST 800-171 3.11.2.

    Checked, not declared. In an AWS Organization, Inspector is usually managed by
    a delegated administrator; an `aws_inspector2_enabler` in the stack would
    either be refused or take over a setting the organization owns, and destroying
    it would turn scanning off. Both reference accounts were found that way on
    2026-10-12.

    `blocked`, not `fail`: the install does not need it, and nothing breaks
    without it. It is owed before the deployment handles CUI.
    """
    title = "Image scanning"
    if identity is None:
        return _no_credentials(CHECK_IMAGE_SCANNING, title)
    try:
        status = world.inspector_ecr_status()
    except Exception as err:  # noqa: BLE001
        if _error_code(err) in _DENIED_CODES:
            return _translate(err, CHECK_IMAGE_SCANNING, title, "inspector2:BatchGetAccountStatus")
        raise

    account = identity.get("Account", "unknown")
    if status in ("ENABLED", "ENABLING"):
        return Record(
            identifier=CHECK_IMAGE_SCANNING,
            title=title,
            state="pass",
            message=f"Amazon Inspector ECR scanning is {status} for account {account} in "
            f"{world.region}, so the images this deployment pulls are scanned in its own account.",
        )
    return Record(
        identifier=CHECK_IMAGE_SCANNING,
        title=title,
        state="blocked",
        message=f"Amazon Inspector ECR scanning is {status or 'not reported'} for account {account} "
        f"in {world.region}. The stack's registry scanning applies without it and then scans "
        "nothing, so this deployment would hold no vulnerability evidence for its images "
        "(NIST 800-171 3.11.2). The install does not need it; a deployment handling CUI does.",
        remedy=Remedy(
            "human",
            "If your AWS Organization manages Amazon Inspector, ask its delegated administrator "
            f"to enable ECR scanning for account {account}. Otherwise run "
            f"`aws inspector2 enable --resource-types ECR --region {world.region}`. "
            "See 'How the images are trusted' in docs/images.md.",
        ),
    )


def _ami_sources(root: Path, repo: Path) -> tuple[str, dict[str, str]] | None:
    """(Globus AMI id, GPU selector tags) as the stack will use them, or None.

    The GPU selector is the three tags in
    `terraform/modules/karpenter/helm-values/gpu-nodeclass.yaml`: two image
    digests from the stack's `module "karpenter"` call, and the EKS version —
    `kubernetes_version` from this root's tfvars if it sets one, else the
    stack's default.
    """
    from . import roots

    try:
        globus_text = (repo / _GLOBUS_CALL).read_text()
        karpenter_text = (repo / _KARPENTER_CALL).read_text()
        variables_text = (repo / _STACK_VARIABLES).read_text()
    except OSError:
        return None

    globus = re.search(r'^\s*globus_ami_id\s*=\s*"(ami-[0-9a-f]+)"', globus_text, re.M)
    fastsurfer = re.search(r'^\s*fastsurfer_ami_digest\s*=\s*"([^"]+)"', karpenter_text, re.M)
    fireants = re.search(r'^\s*fireants_ami_digest\s*=\s*"([^"]+)"', karpenter_text, re.M)
    default = re.search(
        r'variable "kubernetes_version"\s*\{[^}]*?default\s*=\s*"([^"]+)"', variables_text, re.S
    )
    if not (globus and fastsurfer and fireants and default):
        return None

    override, _ = roots.tfvars_assignments(root).get("kubernetes_version", (None, None))
    version = override if isinstance(override, str) and override else default.group(1)
    return globus.group(1), {
        "fastsurfer-image-digest": fastsurfer.group(1),
        "fireants-image-digest": fireants.group(1),
        "eks-version": version,
    }


def _check_globus_inputs(root: Path, repo: Path) -> Record:
    """Whether every one of `globus init`'s variables is set in a file Terraform loads.

    A report, not a requirement this command can satisfy: those variables are the
    Globus tool's to own (design D4). Unset, `terraform plan` prompts for them, or
    fails outright under `-input=false`.

    Set, not *rendered*. This used to pass only when a Globus `*.auto.tfvars`
    existed, and so told a root that sets the variables by hand in
    `terraform.tfvars` — the reference deployment's own, and one the example's
    `terraform.tfvars.example` explicitly allows — that eight variables were unset
    when none was. Terraform does not care which file a value came from, so this
    does not either.

    How many of the eight are checked depends on `globus_enabled`. Seven of them
    are required only when the ingress is on, and demanding them from a
    deployment that creates no Globus resource would be this command reporting a
    prerequisite that does not exist. `globus_s3_destination_bucket` is checked
    either way: despite the name it is the imaging data bucket, which the
    pipeline reads whether or not anything Globus wrote it.
    """
    from . import roots

    title = "Globus inputs"
    variables_tf = repo / "terraform/modules/stack/variables.tf"
    expected = roots.wanted_globus_inputs(root, variables_tf)
    if expected is None:
        return _skipped(
            CHECK_GLOBUS_INPUTS,
            title,
            f"the list of Globus variables could not be derived: {variables_tf} is not readable. "
            "Run this from a clone of the repository.",
            Remedy("human", f"Check that {variables_tf} is present and readable."),
        )

    enabled = roots.globus_ingress_enabled(root)
    found = roots.globus_inputs(root, expected)
    if found.missing:
        missing = len(found.missing)
        where = "in any file Terraform loads from" if missing == len(expected) else "in"
        kind, text = roots.missing_globus_remedy(root, found.missing)
        how = (
            "Render them with `globus init`, or set them by hand in terraform.tfvars as the "
            "example terraform.tfvars.example shows; either satisfies Terraform."
            if kind == "command"
            else "It is set by hand; nothing renders it."
        )
        return Record(
            identifier=CHECK_GLOBUS_INPUTS,
            title=title,
            state="fail",
            message=f"{_count(missing, len(expected))} {_noun(len(expected))} "
            f"{'is' if missing == 1 else 'are'} not set {where} {root}: "
            f"{', '.join(found.missing)}. Terraform would prompt for "
            f"{'it' if missing == 1 else 'them'} — or fail, under -input=false. {how}",
            remedy=Remedy(kind, text),
        )
    ingress = (
        "" if enabled else " (globus_enabled is not true, so the ingress inputs are not needed)"
    )
    every = "the one" if len(expected) == 1 else f"all {len(expected)}"
    return Record(
        identifier=CHECK_GLOBUS_INPUTS,
        title=title,
        state="pass",
        message=f"{every} {_noun(len(expected))} {'is' if len(expected) == 1 else 'are'} "
        f"set{ingress}, in {', '.join(path.name for path in found.sources)}.",
    )


def _noun(count: int) -> str:
    return "Globus variable" if count == 1 else "Globus variables"


def _count(part: int, whole: int) -> str:
    """ "the 1" / "3 of the 8" — `1 of the 1` reads as a counting bug."""
    return "the" if part == whole == 1 else f"{part} of the {whole}"


# --------------------------------------------------------------------------- #
# Shared outcomes
# --------------------------------------------------------------------------- #


def _skipped(identifier: str, title: str, message: str, remedy: Remedy) -> Record:
    return Record(
        identifier=identifier, title=title, state="skipped", message=message, remedy=remedy
    )


def _federation(answers: dict[str, Any]) -> dict[str, Any] | None:
    """The federation block, or None when this deployment federates nothing."""
    block = answers.get("cognito_federation")
    return block if isinstance(block, dict) and block else None


_NO_FEDERATION = (
    "no cognito_federation is set, so this deployment federates no institutional provider."
)


def _check_federation_metadata(world: World, answers: dict[str, Any]) -> Record:
    """The provider's own document is reachable and is the right kind of document.

    For OIDC, the discovery document, and not merely a 200: Cognito needs the
    authorization, token and keyset endpoints, and a document missing one
    passes a status check and fails at sign-in. For SAML, that the metadata URL
    answers at all and looks like metadata — Cognito reads it at apply, so an
    unreachable one is a failed apply rather than a failed login.
    """
    federation = _federation(answers)
    if federation is None:
        return _skipped(
            CHECK_FEDERATION_METADATA,
            "Federated provider metadata",
            _NO_FEDERATION,
            Remedy("human", "Nothing to fix: operators sign in to the Cognito pool itself."),
        )

    saml = federation.get("type") == "saml"
    url = (
        str(federation.get("metadata_url"))
        if saml
        else str(federation.get("oidc_issuer", "")).rstrip("/")
        + "/.well-known/openid-configuration"
    )
    title = "Federated provider metadata"
    try:
        status, body = world.fetch_json(url)
    except (urllib.error.URLError, OSError, ValueError) as err:
        return _skipped(
            CHECK_FEDERATION_METADATA,
            title,
            f"{url} could not be reached from here ({err}). This machine's network, not "
            "necessarily the provider.",
            Remedy("human", f"Open {url} in a browser, or try again from a network that can."),
        )

    if status != 200:
        return Record(
            identifier=CHECK_FEDERATION_METADATA,
            title=title,
            state="fail",
            message=f"{url} returned HTTP {status}.",
            remedy=Remedy(
                "human",
                "Confirm the URL with the institution's identity team. For OIDC it is the "
                "`issuer`, not a login or authorization URL.",
            ),
        )

    if saml:
        # Not JSON: fetch_json returns None for a body it cannot parse, which is
        # what metadata XML looks like from here. That is the pass.
        return Record(
            identifier=CHECK_FEDERATION_METADATA,
            title=title,
            state="pass",
            message=f"{url} answers, and Cognito will read it at apply.",
        )

    if not isinstance(body, dict):
        return Record(
            identifier=CHECK_FEDERATION_METADATA,
            title=title,
            state="fail",
            message=f"{url} did not return a discovery document.",
            remedy=Remedy("human", "Confirm the issuer URL with the institution's identity team."),
        )

    needed = ("authorization_endpoint", "token_endpoint", "jwks_uri")
    absent = [key for key in needed if not body.get(key)]
    if absent:
        return Record(
            identifier=CHECK_FEDERATION_METADATA,
            title=title,
            state="fail",
            message=f"{url} publishes no {', '.join(absent)}. Cognito discovers the endpoints "
            "from this document, so a missing one is a sign-in that cannot complete.",
            remedy=Remedy("human", "Ask the institution's identity team for the endpoints."),
        )
    return Record(
        identifier=CHECK_FEDERATION_METADATA,
        title=title,
        state="pass",
        message=f"{url} publishes all three endpoints Cognito needs.",
    )


def _check_federation_evidence(answers: dict[str, Any]) -> Record:
    """Exactly one declared form of multifactor evidence, and what it commits to.

    Terraform refuses the configuration without one, so this is not the only
    gate. It is here because the deployer should meet the requirement while
    reading a report, not while reading a plan error — and because the
    attestation form is a claim about an institution's policy that a human has
    to stand behind, which is worth printing back to them.
    """
    federation = _federation(answers)
    if federation is None:
        return _skipped(
            CHECK_FEDERATION_EVIDENCE,
            "Federated MFA evidence",
            _NO_FEDERATION,
            Remedy("human", "Nothing to fix: the pool requires an authenticator app of its own."),
        )

    evidence = federation.get("mfa_evidence") or {}
    by_claim = bool(evidence.get("claim")) and bool(evidence.get("claim_value"))
    by_attestation = bool(evidence.get("attestation"))

    if by_claim == by_attestation:
        return Record(
            identifier=CHECK_FEDERATION_EVIDENCE,
            title="Federated MFA evidence",
            state="fail",
            message="cognito_federation.mfa_evidence declares "
            + ("both forms" if by_claim else "neither form")
            + ". Cognito delegates authentication to the provider and adds no second factor of "
            "its own, so exactly one of them is what satisfies NIST 800-171 3.5.3.",
            remedy=Remedy(
                "human",
                "Set either `claim` and `claim_value` (a per-login claim the Access policy will "
                "require) or `attestation` (a reference to their written MFA policy).",
            ),
        )

    if by_claim:
        return Record(
            identifier=CHECK_FEDERATION_EVIDENCE,
            title="Federated MFA evidence",
            state="pass",
            message=f"a login must carry {evidence['claim']} = {evidence['claim_value']}, and the "
            "Access policy requires it.",
        )
    return Record(
        identifier=CHECK_FEDERATION_EVIDENCE,
        title="Federated MFA evidence",
        state="pass",
        message=f"attested: {evidence['attestation']}. Nothing is enforced at login, so your "
        "SSP cites this and someone has to keep it true.",
    )


def _check_federation_secret(
    world: World, answers: dict[str, Any], identity: dict[str, Any] | None
) -> Record:
    """The OIDC client secret the deployer creates by hand exists.

    Read rather than created, like every other hand-made secret here, so an
    absent one fails at apply — the failure this check exists to move earlier.
    """
    federation = _federation(answers)
    title = "Federated client secret"
    if federation is None or federation.get("type") != "oidc":
        return _skipped(
            CHECK_FEDERATION_SECRET,
            title,
            _NO_FEDERATION
            if federation is None
            else "a SAML provider needs no client secret: its trust is the metadata document.",
            Remedy("human", "Nothing to fix."),
        )

    name = str(federation.get("client_secret_id") or "")
    if not name:
        return Record(
            identifier=CHECK_FEDERATION_SECRET,
            title=title,
            state="fail",
            message="cognito_federation.client_secret_id is not set, so there is no secret for "
            "Terraform to read the institution's client credentials from.",
            remedy=Remedy("human", "Set it to the name of the secret you created."),
        )
    if identity is None:
        return _no_credentials(CHECK_FEDERATION_SECRET, title)

    try:
        world.describe_secret(name)
    except Exception as err:  # noqa: BLE001
        if _error_code(err) in _DENIED_CODES:
            return _translate(err, CHECK_FEDERATION_SECRET, title, "secretsmanager:DescribeSecret")
        return Record(
            identifier=CHECK_FEDERATION_SECRET,
            title=title,
            state="fail",
            message=f"no Secrets Manager secret named {name} in account "
            f"{identity.get('Account', 'unknown')}, region {world.region}. Terraform reads it "
            "and never creates it, so the apply fails on the data source.",
            remedy=Remedy(
                "command",
                f"aws secretsmanager create-secret --name {name} "
                '--secret-string \'{"clientID":"...","clientSecret":"..."}\'',
            ),
        )
    return Record(
        identifier=CHECK_FEDERATION_SECRET,
        title=title,
        state="pass",
        message=f"{name} exists in region {world.region}.",
    )


def _no_credentials(identifier: str, title: str) -> Record:
    return _skipped(
        identifier,
        title,
        "no AWS credentials resolved, so this could not be checked.",
        Remedy("command", "aws sso login"),
    )


def _translate(err: Exception, identifier: str, title: str, action: str) -> Record:
    """A denied read becomes `skipped` naming the action; anything else is a failure.

    The distinction is the whole requirement. "I was not allowed to look" and "I
    looked and the answer is no" lead a deployer to two different people.
    """
    code = _error_code(err)
    if code in _DENIED_CODES:
        return _skipped(
            identifier,
            title,
            f"this check needs {action}, which these credentials do not have ({code}).",
            Remedy(
                "human",
                f"Ask whoever owns your permission set to add {action}, or run preflight as a "
                "principal that has it. The apply itself does not need you to hold it.",
            ),
        )
    if code in _EXPIRED_CODES:
        return _skipped(
            identifier,
            title,
            f"the AWS session expired while running this check ({code}).",
            Remedy("command", "aws sso login"),
        )
    return Record(
        identifier=identifier,
        title=title,
        state="fail",
        message=f"{action} failed: {err}",
        remedy=Remedy("human", f"Resolve the error above, or run {action} by hand to see it."),
    )


def _cloudflare_errors(body: Any) -> str:
    """The `errors[].message` text from a Cloudflare v4 envelope, or "".

    Shown so a refusal names Cloudflare's own reason rather than only a status.
    Cloudflare's error messages describe the request, never the credential, so
    nothing here can carry the token.
    """
    if not isinstance(body, dict):
        return ""
    messages = [
        str(entry.get("message"))
        for entry in body.get("errors") or ()
        if isinstance(entry, dict) and entry.get("message")
    ]
    return "; ".join(messages)


def _error_code(err: Exception) -> str:
    """A botocore error code, without importing botocore.

    Duck-typed on `response` because importing `botocore.exceptions` here would
    make this module's import cost the SDK's — and `World` imports boto3 lazily
    precisely so that a run which touches no AWS call does not pay it.
    """
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", "") or "")
    return ""

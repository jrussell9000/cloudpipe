"""The Globus login flow: what it asks for, and what it refuses to store.

Replaces `images/globus/setup_auth.py`, whose flow was copy-paste-a-code into a
terminal. The mechanics that matter are not cosmetic:

**`prompt=login` plus `session_required_single_domain`.** A High Assurance
collection is not satisfied by *having* a token; it requires a recent
authentication event in an allowed identity domain. Without both parameters,
Globus is free to reuse an existing browser session that does not satisfy the
policy, and the failure surfaces much later as a 403 during a transfer — which is
what happened to a 300-subject batch on 2026-08-17.

**Refresh tokens do not extend a session.** The refresh token keeps working while
the *session* behind it has lapsed, so nothing fails until a High Assurance
collection is touched. That is why a login time is recorded rather than inferred
from the token.

**Nothing is written to disk.** The token lives in a `MemoryTokenStorage` we own,
is copied into Secrets Manager, and goes away with the process. `GlobusApp`'s
default is a JSON file in the user's home directory, which would leave a
long-lived transfer credential on every workstation that ever ran setup.

**The identity is checked before anything is stored.** A login as the wrong
identity produces a token that authenticates fine and then cannot read the source
collection — a failure that looks like a broken gateway. Refusing at login is the
only place the mistake is still cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .exits import CliError, ExitCode, Remedy

TRANSFER_RESOURCE_SERVER = "transfer.api.globus.org"
AUTH_RESOURCE_SERVER = "auth.globus.org"
APP_NAME = "cloudpipe-globus-admin"


@dataclass(frozen=True)
class LoginResult:
    """What a successful login produced. The token is stored, never printed."""

    client_id: str
    refresh_token: str
    username: str
    identity_id: str | None
    established_at: datetime

    def secret_payload(self) -> dict[str, str]:
        """Exactly the shape `transfer.py` and the ExternalSecret already expect."""
        return {"native-app-client-id": self.client_id, "refresh-token": self.refresh_token}


@dataclass
class LoginSession:
    """One login attempt, with its own in-memory token store.

    Built as an object rather than a function so a test can drive the same steps
    with a stub app: the order of the steps (log in, identify, extract, and only
    then store) is the part worth testing.
    """

    client_id: str
    domains: tuple[str, ...]
    no_browser: bool = False
    app_factory: Callable[..., Any] | None = None
    userinfo: Callable[[Any], dict[str, Any]] | None = None
    now: Callable[[], datetime] | None = None

    def run(self) -> LoginResult:
        storage = _memory_storage()
        app = (self.app_factory or _build_app)(
            client_id=self.client_id, no_browser=self.no_browser, storage=storage
        )

        # `force=True` because a login that silently reuses a valid-looking token
        # is the one thing this command must never do: it is run precisely when
        # the session behind that token has lapsed.
        app.login(auth_params=self._auth_params(), force=True)

        username, identity_id = self._identify(app)
        self._require_allowed_domain(username)

        token_data = storage.get_token_data(TRANSFER_RESOURCE_SERVER)
        refresh_token = getattr(token_data, "refresh_token", None) if token_data else None
        if not refresh_token:
            raise CliError(
                "globus.no_refresh_token",
                "The login succeeded but returned no refresh token, so the pipeline would "
                "have no way to renew its access.",
                exit_code=ExitCode.CHECK_FAILED,
                remedy=Remedy(
                    "human",
                    "check that the Globus app is registered as a native app that may "
                    "request refresh tokens",
                ),
            )

        moment = (self.now or (lambda: datetime.now(UTC)))()
        return LoginResult(
            client_id=self.client_id,
            refresh_token=str(refresh_token),
            username=username,
            identity_id=identity_id,
            established_at=moment,
        )

    def _auth_params(self) -> Any:
        return auth_params(self.domains)

    def _identify(self, app: Any) -> tuple[str, str | None]:
        try:
            info = (self.userinfo or userinfo_of)(app)
        except Exception as exc:  # pragma: no cover - network failures
            from . import errors

            raise errors.globus_error(exc) from exc
        username = str(info.get("preferred_username") or info.get("email") or "").strip()
        identity_id = info.get("sub")
        return username, str(identity_id) if identity_id else None

    def _require_allowed_domain(self, username: str) -> None:
        require_allowed_domain(username, self.domains)


def auth_params(domains: tuple[str, ...]) -> Any:
    """The forced-reauthentication parameters, for any login this package performs.

    Module-level rather than a method because a second login exists that is not a
    `LoginSession`: `service_client.ManageProjectsSession` asks for one scope, wants
    no refresh token, and stores nothing — but must force re-authentication for the
    same reasons, and a second copy of these two parameters is how one of them
    quietly loses `prompt=login`.
    """
    from globus_sdk.gare import GlobusAuthorizationParameters

    return GlobusAuthorizationParameters(
        prompt="login",
        session_required_single_domain=list(domains) or None,
    )


def require_allowed_domain(username: str, domains: tuple[str, ...]) -> None:
    """Refuse a login as an identity outside the deployment's declared domains.

    Belt and braces beside `session_required_single_domain`, which Globus enforces:
    this reads the identity that came back. Shared with the service-client login
    for a narrower reason than the transfer token's — a client registered under a
    personal identity is a client that leaves when its owner does.
    """
    if not domains:
        return
    domain = username.rpartition("@")[2].lower()
    if domain and domain in {d.lower() for d in domains}:
        return
    raise CliError(
        "globus.identity_wrong_domain",
        f"Logged in as {username or 'an unknown identity'}, which is not in this "
        f"deployment's allowed domain(s): {', '.join(domains)}. Nothing was "
        "stored — a token for the wrong identity authenticates fine and then cannot "
        "read the source collection, which is far harder to diagnose later.",
        exit_code=ExitCode.CHECK_FAILED,
        remedy=Remedy(
            "human",
            f"run the login again and sign in with an identity in {', '.join(domains)}",
        ),
        detail={"username": username, "allowed_domains": list(domains)},
    )


def resolve_client_id(
    explicit: str | None, config_value: str | None, env_value: str | None, stored: str | None
) -> str:
    """Where the native app client id comes from, in order of how explicit it is.

    The stored value is last and matters most in practice: after the first login
    nobody has to find the client id again, which is the difference between a
    routine re-login and a scavenger hunt through an old runbook.
    """
    for candidate in (explicit, config_value, env_value, stored):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    raise CliError(
        "config.client_id_missing",
        "No Globus native app client id is configured, so there is no application to log in to.",
        exit_code=ExitCode.INVALID,
        remedy=Remedy(
            "human",
            "add `native_app_client_id` to the answers document, or pass --client-id "
            "(register one at https://app.globus.org/settings/developers)",
        ),
    )


def _memory_storage() -> Any:
    from globus_sdk.token_storage import MemoryTokenStorage

    return MemoryTokenStorage()


def _build_app(*, client_id: str, no_browser: bool, storage: Any) -> Any:
    import globus_sdk

    config = globus_sdk.GlobusAppConfig(
        # Ours, held here — not the default JSON file in the user's home.
        token_storage=storage,
        login_flow_manager="command-line" if no_browser else "local-server",
        request_refresh_tokens=True,
    )
    return globus_sdk.UserApp(
        APP_NAME,
        client_id=client_id,
        config=config,
        scope_requirements={
            TRANSFER_RESOURCE_SERVER: [globus_sdk.TransferClient.scopes.all],
            AUTH_RESOURCE_SERVER: ["openid", "profile", "email"],
        },
    )


def userinfo_of(app: Any) -> dict[str, Any]:
    """The logged-in identity, as a plain dict.

    `dict(response)` reads as obvious and is wrong: a `GlobusHTTPResponse`
    defines `__getitem__` but no `keys()`, so `dict()` falls back to the
    sequence protocol, asks for index `0`, and raises `KeyError: 0`. That
    reached the operator as a login failing with the bare text `error: 0`,
    after the browser flow had already succeeded — see the test below. Read
    `.data`, which is the parsed JSON body.
    """
    import globus_sdk

    response = globus_sdk.AuthClient(app=app).userinfo()
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return dict(data)
    if isinstance(response, dict):
        return dict(response)
    raise CliError(
        "globus.userinfo_unreadable",
        "Globus returned an identity response this tool could not read, so the login "
        f"cannot be attributed to an identity ({type(response).__name__}). Nothing was stored.",
        exit_code=ExitCode.ERROR,
        remedy=Remedy("human", "report this with the globus-sdk version in `pixi.lock`"),
    )

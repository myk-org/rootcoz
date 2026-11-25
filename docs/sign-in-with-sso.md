# Sign in with SSO

RootCoz can sign people in through an identity provider instead of — or alongside
— a username and API key. This page covers what an administrator configures and
what a person experiences.

Four providers are built in: **Google**, **GitHub**, **OpenShift** and **Red Hat
SSO** (a Keycloak realm). A provider is offered only when *every* one of its
settings is present, so a half-configured provider never renders a button that
cannot work.

Two of them are OpenID Connect — **Google** and **Red Hat SSO** — and two are
plain OAuth 2.0 — **GitHub** and **OpenShift**. That split is deliberate, not an
unfinished migration:

- Google and Red Hat SSO publish discovery metadata with a JWKS and answer the
  code exchange with an **ID token**, so the sign-in is verified end to end
  (signature, issuer, audience, `azp`, expiry, and the nonce that binds the token
  to this attempt).
- GitHub has no ID token at all: the access token is spent against `/user` and
  `/user/emails`, which is the only place a GitHub account exists.
- An OpenShift cluster's built-in OAuth server is **not** an OIDC provider. It
  publishes OAuth 2.0 metadata (no JWKS, no ID token, no `userinfo_endpoint`) and
  issues **opaque** tokens; the account — its name, its identities and its email —
  lives on the cluster API server, reachable with that token and nowhere else.
  Demanding OIDC of it would not harden the login, it would remove the provider.

## Before you start

`PUBLIC_BASE_URL` must be set. The OAuth callback and the post-logout redirect are
built from it and never from request headers, so a deployment behind a proxy has
to name the origin people actually reach it at.

For each provider, register two things at the IdP:

| What to register | Value |
| --- | --- |
| Authorized redirect URI | `{PUBLIC_BASE_URL}/api/auth/sso/{provider}/callback` |
| Post-logout redirect URI | `{PUBLIC_BASE_URL}/login` — only needed to end the IdP session on sign-out |

`{provider}` is one of `google`, `github`, `openshift`, `redhat` — for example
`https://rootcoz.example.com/api/auth/sso/redhat/callback`.

## Per-provider setup

### Google

Create an OAuth client in the Google Cloud Console and copy the client id and
secret into Server Settings → Auth & Security (under the collapsible **Google**
section). Google publishes its endpoints at a fixed issuer, so there is nothing
else to configure.

### GitHub

Create a GitHub OAuth App with the callback above. The `user:email` scope is
required: without it GitHub answers its email endpoint with a `403` and the login
fails outright. Only `github.com` / `api.github.com` are supported — a GitHub
Enterprise Server installation serves its own endpoints and is not supported yet.

### OpenShift

Create an OAuth client on the cluster (`oauthclients.oauth.openshift.io`) and set
two origins. `SSO_OPENSHIFT_ISSUER` is the cluster's **OAuth server**
(`https://oauth-openshift.apps.<cluster>`): it is asked for exactly two things,
`/oauth/authorize` and `/oauth/token`, so it is the code exchange and nothing
else. It must be `https` with no path, no query and no `/.well-known/…`; `http` is
accepted only for a loopback development server.

`SSO_OPENSHIFT_API_URL` is the cluster **API server** (`https://api.<cluster>:6443`),
and it is where the account, its identities and its email are read — with the
opaque access token the OAuth server just issued. That is why both settings are
required: with only the OAuth origin set, nothing can name the account, and every
login fails at the account lookup. The API URL is held to the same shape as the
origin — `https`, no path, `http` only for a loopback server.

Neither value is an OIDC issuer, so there is no `iss` claim to match: a cluster's
OAuth server issues opaque access tokens and never mints an ID token. It publishes
no `email_verified` claim either, so if you use the domain allow-list the address
itself decides: an identity object is written by the cluster admin (or by the IdP
mapping the admin configured), never by the end user, so the address it carries
is authoritative and admitted on its domain alone.

> **RBAC limitation — email and `SSO_ALLOWED_DOMAINS`:** The `user:info` scope
> grants `get` on `users/~` but typically does **not** grant cluster-scoped `get`
> on `identities` resources. When the user's access token lacks that permission,
> the identity lookup returns a 403, no email address is available, and
> `SSO_ALLOWED_DOMAINS` will refuse every OpenShift login with
> `domain_not_allowed`. To use a domain allow-list with OpenShift, either grant
> read access to `identities` for authenticated users on the cluster, leave
> `SSO_ALLOWED_DOMAINS` empty, or use an OIDC provider (Red Hat SSO or Google)
> that returns the email in the ID token.

### Red Hat SSO (Keycloak)

Create a client in the realm, set `SSO_REDHAT_ISSUER` to the realm's issuer URL,
and add `{PUBLIC_BASE_URL}/login` to the client's *Valid post logout redirect
URIs* if you want signing out to end the Keycloak session too.

`SSO_REDHAT_ISSUER` is a real OIDC issuer — a Keycloak realm — so it must be
**exactly the value the realm puts in its ID token's `iss` claim**. The realm path
(`/realms/<realm>`) belongs to that value; a `/.well-known/…` suffix never does.
It must be `https`, and plaintext is accepted only for a loopback development
server. A malformed issuer is rejected when the setting is saved, rather than
silently removing the button.


### What verifies the tokens

The OIDC providers talk to their IdP directly (`httpx`), and ID tokens are
verified with `joserfc`: the signing algorithm is pinned to RS256, the key comes
from the issuer's JWKS, and `iss`, `aud`, `azp`, `exp`, `iat` and `nonce` are each
checked explicitly — a token that omits `exp` or `iat` is refused, not waved
through. This is deliberately not built on Authlib — the checks that matter
here are the claim-level ones this repository needs to state in one place (a
stolen token from a sibling client, an unverified address, a nonce from another
attempt), and the discovery document is pinned to the configured issuer with every
endpoint required to be https. Authlib was dropped from the dependencies because
nothing used it.

An OIDC provider that answers the code exchange **without** an ID token is
refused (`no_id_token`), not quietly downgraded. The `userinfo` endpoint is
authenticated by the access token alone: it carries no signature and no nonce, so
it cannot tell a fresh sign-in from a token replayed out of an earlier attempt.
Both OIDC providers here always return an ID token, so nothing loses anything — a
provider that genuinely cannot mint one is supported by its own module (as
OpenShift is), never by a weaker path through the OIDC one.

## How a sign-in works

1. The person picks a provider on the login page and is sent to it with a sealed
   state cookie (CSRF nonce, PKCE verifier, nonce and destination).
2. The provider redirects back. RootCoz verifies the state, exchanges the code and
   — for the two OIDC providers — verifies the ID token: signature, issuer,
   audience, expiry and nonce.
3. The identity is looked up **by `(provider, subject)` only**. The username and
   the email are never used to find an account: a local `ruth` and an IdP `ruth`
   can be different people, and one person can be `ruth` in one place and
   `ruthn` in another.
4. A known identity gets a session. An unknown one gets no session at all — the
   first-login screen asks for a username and clicking **Continue** checks it
   against that bound ticket: if an account with that name already exists, it
   prompts for that account's API key to prove ownership and link the identity
   (no re-approval needed if active); otherwise it creates a new account under
   that name.

New accounts follow the same rules as registration: `ALLOWED_USERS`, the username
rules, `REQUIRE_APPROVAL` (so a new account may land on the pending page), and the
provider's ability to create accounts at all. Email is never used to merge two
accounts.

## Restricting who may sign in

`SSO_ALLOWED_DOMAINS` is the deployment's own admission rule, independent of any
provider: a comma-separated list such as `redhat.com,example.org`. An entry covers
that domain and its real subdomains, so `alice@eng.redhat.com` passes for
`redhat.com` while `notredhat.com` and `redhat.com.evil.io` do not.

It is checked on **every** SSO sign-in, before any account is looked up, so
removing a domain also cuts off an account that is already linked — on its next
sign-in. An address is admitted only when it is **positively verified** (the
provider asserts `email_verified` as `true`) or **authoritative** (only the
deployment's IdP or cluster admin can write it — which is how an OpenShift
identity is admitted, since its cluster publishes no `email_verified` claim); a
login whose address is missing or neither of those is refused. It never applies
to username + API key logins or to `ADMIN_KEY`, so an administrator cannot lock
themselves out with it.

The domain list decides *who may sign in*; `REQUIRE_APPROVAL` decides *when they
may act*. Pair them when the provider's tenant is wider than your team.

## Connecting more than one provider

Profile → **Sign-in methods** lists the providers connected to your account and
lets you connect another or disconnect one. An account with none connected gets a
one-line prompt on the dashboard — only when the deployment offers SSO and only to
someone who may connect one; dismissing it hides it for that browser session.

Three rules matter:

- **Attaching a provider to an existing account asks first** (when claiming via
  the first-login choice screen; the API-key link screen on the settings page
  connects directly without an extra confirmation step).
- **The last sign-in method cannot be removed from a keyless account**, by you or
  by an administrator. An account created through a trusted proxy or SSO may
  never have been shown its API key, so unlinking the only identity from a
  keyless account would lock you out. When the account holds a local API key,
  unlinking the sole SSO provider is allowed because local username + API-key
  login remains. Disconnecting the sole SSO provider on a keyed account prompts
  for confirmation to verify you still have your API key saved (or to rotate it
  first). Connect another provider, generate an API key, or rotate the
  key and keep it, first.
- An identity that already belongs to **another** account is refused without
  naming that account, since the answer would otherwise confirm that an address
  has an account here.

Connecting is a **button on the settings page, never a link** you can be sent to.
The card `POST`s the start from the page itself, and the server requires proof
that the request came from this deployment (`Sec-Fetch-Site: same-origin`, or a
matching `Origin`/`Referer`): a signed-in browser is not consent, so a cross-site
navigation cannot start a connection that a provider session might then authorise
silently. A plain `GET` carrying `mode=link` is refused (`link_requires_post`) and
a cross-site `POST` is refused (`link_cross_site`) — neither starts a flow.

Linking is also re-checked when it finishes, not just when it starts: the account
must still exist, still be a reviewer, and still be usable. A **disabled,
pending or rejected** account cannot complete a connection, even from a session
that is still valid — and an administrator disabling an account takes effect
immediately rather than when that session expires.

Administrators can inspect and change anyone's sign-in methods under
**Admin → Users**, and from the CLI:

```bash
rootcoz auth sso-identities
rootcoz auth sso-unlink github
rootcoz admin users identities alice
rootcoz admin users unlink-identity alice github
```

## Signing in from a terminal

`rootcoz auth sso` uses a device flow: it prints a short code and the approval
page, you approve it in a browser where you are already signed in, and the
terminal receives a **session token** — bounded by `SESSION_TTL_HOURS`, not a
replacement API key, so signing in on a second machine never invalidates the
first. Removing the token from local config does not revoke its server session;
to revoke an exposed terminal token early, rotate your API key
(`rootcoz auth rotate-key`, which invalidates all sessions for the account) or
have an administrator disable the account. The code grants nothing until a
person approves it, the approval page shows which client is asking, the server
refuses polls faster than the advertised interval, and an approval only counts
when it comes from this server's own page (a cookie-authenticated approval with
no same-origin proof is refused, while `rootcoz auth sso-approve` sends a Bearer
credential and needs none).

The account that approves is the account the terminal becomes — the bootstrap
`admin` included, which has no profile row and is still a legitimate approver.

```bash
rootcoz auth sso --provider google
rootcoz auth sso-approve ABCD-EFGH   # approve from a terminal you are signed in on
```

## Signing out

Signing out deletes the local session and, where the provider supports it, sends
the browser on to end the provider's session too. Google and GitHub have no
end-session endpoint, so there the provider's own session survives — use a private
window or sign out at the provider if that matters. OpenShift has none either, so
there signing out is local only.

No `id_token_hint` is sent: the flow never stores the ID token. Keycloak accepts
the call without it, which is why the client id is included in the URL.

## Trusted proxy instead of SSO

A deployment that already authenticates everyone at a proxy can let that proxy
name the user: set `TRUST_PROXY_HEADERS=true` and the proxy sends
`X-Forwarded-User`. The proxy is then the authentication boundary, so only enable
this behind a proxy you control.

The first request from a name with no account **provisions** it:
`created_via='proxy'`, the role from `DEFAULT_USER_ROLE`, and the status every new
account gets — `pending` while `REQUIRE_APPROVAL` is on, `active` when it is off.
The proxy proves *who* someone is, not that they may act: `created_via` is
provenance for operators and the audit trail — it records only how the row came to
exist (`internal`, `admin`, `proxy`, or the SSO provider that created it) — while
the role, the approval status, `can_view_reports`, `can_use_server_providers` and
the admin bit are decided by other columns and settings, so `'proxy'` or `'sso'`
is never a role, an approval, or a reason to trust an account. Each provisioning
is audited as `auth.proxy_provisioned`, `ALLOWED_USERS` bounds it like any other
creation, and reserved names (`admin`, `rootcoz*`) are never accepted from the
header.

A proxy-authenticated visitor on the login page is sent straight to where their
account stands: `/` when it may act, `/pending` while approval holds it, and
nowhere — the login page is served — when the resource is blocked (`disabled`,
`locked`, `rejected`) or when the name is one provisioning would refuse
(malformed, reserved, or outside `ALLOWED_USERS`). The redirect asks the same
question provisioning does, so a name that will never get an account cannot be
sent to a page that refuses it and bounced back here forever; the refusal is
audited as `auth.proxy_provision_refused`.

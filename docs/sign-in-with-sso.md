# Sign in with SSO

RootCoz can sign people in through an identity provider instead of — or alongside
— a username and API key. This page covers what an administrator configures and
what a person experiences.

Four providers are built in: **Google**, **GitHub**, **OpenShift** and **Red Hat
SSO** (a Keycloak realm). A provider is offered only when *every* one of its
settings is present, so a half-configured provider never renders a button that
cannot work.

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
secret into Server Settings → SSO. Google publishes its endpoints at a fixed
issuer, so there is nothing else to configure.

### GitHub

Create a GitHub OAuth App with the callback above. The `user:email` scope is
required: without it GitHub answers its email endpoint with a `403` and the login
fails outright. Only `github.com` / `api.github.com` are supported — a GitHub
Enterprise Server installation serves its own endpoints and is not supported yet.

### OpenShift

Create an OAuth client on the cluster (`oauthclients.oauth.openshift.io`) and set
`SSO_OPENSHIFT_ISSUER` to the cluster's issuer URL. An OpenShift OAuth server
publishes no `email_verified` claim, so if you use the domain allow-list it is the
address alone that is checked there.

### Red Hat SSO (Keycloak)

Create a client in the realm, set `SSO_REDHAT_ISSUER` to the realm's issuer URL,
and add `{PUBLIC_BASE_URL}/login` to the client's *Valid post logout redirect
URIs* if you want signing out to end the Keycloak session too.

**The issuer must be exactly the value the provider puts in its ID token's `iss`
claim** — no `/.well-known/…` suffix and no extra path — and it must be `https`.
Plaintext is accepted only for a loopback development server. A malformed issuer
is rejected when the setting is saved, rather than silently removing the button.

## How a sign-in works

1. The person picks a provider on the login page and is sent to it with a sealed
   state cookie (CSRF nonce, PKCE verifier, nonce and destination).
2. The provider redirects back. RootCoz verifies the state, exchanges the code and
   verifies the ID token — signature, issuer, audience, expiry and nonce.
3. The identity is looked up **by `(provider, subject)` only**. The username and
   the email are never used to find an account: a local `ruth` and an IdP `ruth`
   can be different people, and one person can be `ruth` in one place and
   `ruthn` in another.
4. A known identity gets a session. An unknown one gets no session at all — the
   choice screen instead offers to **claim an existing account** (by proving it
   with that account's own username and API key) or to **create a new one** with a
   name the person chooses.

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
sign-in. While the list is set, a login whose address is missing or unverified is
refused as well. It never applies to username + API key logins or to `ADMIN_KEY`,
so an administrator cannot lock themselves out with it.

The domain list decides *who may sign in*; `REQUIRE_APPROVAL` decides *when they
may act*. Pair them when the provider's tenant is wider than your team.

## Connecting more than one provider

Profile → **Sign-in methods** lists the providers connected to your account and
lets you connect another or disconnect one. An account with none connected gets a
one-line prompt on the dashboard — only when the deployment offers SSO and only to
someone who may connect one; dismissing it hides it for that browser session.

Two rules matter:

- **The last sign-in method cannot be removed**, by you or by an administrator.
  Every account holds an API key, but it is shown only once — at registration, or
  at the moment an SSO account is created — and it may never have been saved, so
  removing the only way in could lock you out. Connect another provider, or rotate
  the key and keep it, first.
- An identity that already belongs to **another** account is refused without
  naming that account, since the answer would otherwise confirm that an address
  has an account here.

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
terminal receives a **session token** — bounded and individually revocable, not a
replacement API key, so signing in on a second machine never invalidates the
first. The code grants nothing until a person approves it, the approval page shows
which client is asking, and the server refuses polls faster than the advertised
interval.

```bash
rootcoz auth sso --provider google
rootcoz auth sso-approve ABCD-EFGH   # approve from a terminal you are signed in on
```

## Signing out

Signing out deletes the local session and, where the provider supports it, sends
the browser on to end the provider's session too. Google and GitHub have no
end-session endpoint, so there the provider's own session survives — use a private
window or sign out at the provider if that matters.

No `id_token_hint` is sent: the flow never stores the ID token. Keycloak and OpenShift
accept the call without it, which is why the client id is included in the URL.

## Trusted proxy instead of SSO

A deployment that already authenticates everyone at a proxy can let that proxy
name the user: set `TRUST_PROXY_HEADERS=true` and the proxy sends
`X-Forwarded-User`. The proxy is then the authentication boundary, so only enable
this behind a proxy you control.

The first request from a name with no account **provisions** it:
`created_via='proxy'`, the role from `DEFAULT_USER_ROLE`, and the status every new
account gets — `pending` while `REQUIRE_APPROVAL` is on, `active` when it is off.
The proxy proves *who* someone is, not that they may act. Each provisioning is
audited as `auth.proxy_provisioned`, `ALLOWED_USERS` bounds it like any other
creation, and reserved names (`admin`, `rootcoz*`) are never accepted from the
header.

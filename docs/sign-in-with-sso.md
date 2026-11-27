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
  issues **opaque** tokens; the account lives on the cluster API server,
  reachable with that token and nowhere else.
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

Create a cluster-scoped `OAuthClient` (`oauthclients.oauth.openshift.io`) and
set two origins. `SSO_OPENSHIFT_ISSUER` is the cluster's **OAuth server**
(`https://oauth-openshift.apps.<cluster>`): it is asked for exactly two things,
`/oauth/authorize` and `/oauth/token`, so it is the code exchange and nothing
else. It must be `https` with no path, no query and no `/.well-known/…`; `http` is
accepted only for a loopback development server.

`SSO_OPENSHIFT_API_URL` is the cluster **API server** (`https://api.<cluster>:6443`),
and it is where the account is read — with the opaque access token the OAuth
server just issued. That is why both settings are required: with only the OAuth
origin set, nothing can name the account, and every login fails at the account
lookup. The API URL is held to the same shape as the origin — `https`, no path,
no query, `http` only for a loopback server.

Neither value is an OIDC issuer, so there is no `iss` claim to match: a
cluster's OAuth server issues opaque access tokens and never mints an ID token.
It publishes no `email_verified` claim either (an admin- or IdP-written cluster
identity email is treated as authoritative), and because RootCoz requests only
the `user:info` scope, `identities` lookups return `403` and no email arrives —
see **Scope limitation: email and `SSO_ALLOWED_DOMAINS`** below.

#### Create a cluster-scoped OAuthClient

Creating an `OAuthClient` requires cluster-admin privileges. OpenShift does not
auto-generate a secret for an `OAuthClient`, so generate one explicitly. RootCoz
posts `grant_type=authorization_code` with `client_id` and `client_secret` to
`/oauth/token`; do not use a namespace-scoped `ServiceAccount` OAuth client — it
has no standalone generated OAuth client secret (it authenticates with a
ServiceAccount token, which OpenShift 4.11+ stopped auto-generating, and fails
at `/oauth/token` with `unauthorized_client` when given an arbitrary secret),
cannot use `grantMethod: auto`, and reuses a Kubernetes ServiceAccount
credential. Use a dedicated cluster-scoped `OAuthClient` with explicit
`scopeRestrictions` instead.

Generate a secret and create the `OAuthClient` resource (replacing
`https://rootcoz.example.com` with your `PUBLIC_BASE_URL` so any path prefix or
non-default port is preserved):

```bash
(
  set -eu
  SECRET=$(openssl rand -hex 32)
  oc create -f - <<EOF
apiVersion: oauth.openshift.io/v1
kind: OAuthClient
metadata:
  name: rootcoz
secret: "${SECRET}"
grantMethod: auto
scopeRestrictions:
  - literals:
      - user:info
redirectURIs: ["https://rootcoz.example.com/api/auth/sso/openshift/callback"]
EOF
)
```

`scopeRestrictions` restricts this client to the single read-only `user:info`
literal scope that RootCoz requests, which is why `grantMethod: auto` is safe
for this first-party client. Using `oc create` fails safely with `AlreadyExists`
if re-run (preventing accidental secret rotation) and does not store a plaintext
copy of `secret` in `kubectl.kubernetes.io/last-applied-configuration`, while
the subshell avoids leaving `SECRET` set in your interactive shell.

To copy the secret into RootCoz Server Settings without printing it to terminal
scrollback, pipe it directly to your clipboard (for example with `pbcopy`,
`wl-copy`, or `xclip -selection clipboard`):

```bash
oc get oauthclient rootcoz -o jsonpath='{.secret}' | pbcopy
```

Or print it explicitly with a trailing newline and clear your terminal
afterward:

```bash
oc get oauthclient rootcoz -o jsonpath='{.secret}{"\n"}'
```

> **Secret rotation:** If you ever rotate `.secret` on the `OAuthClient`, update
> `SSO_OPENSHIFT_CLIENT_SECRET` in RootCoz at the same time (otherwise logins
> fail with `exchange_failed`).

`OAuthClient` resources are cluster-scoped. If you run separate RootCoz
deployments against the same cluster (for example, production and development),
create a separate `OAuthClient` with a distinct `metadata.name` and a separate
secret for each deployment.

#### Configure RootCoz

Open **Server Settings → Auth & Security → OpenShift** and fill in all four
settings (see [Configuration Reference](configuration-reference.html) for full
details):

| Setting | Value |
| --- | --- |
| `SSO_OPENSHIFT_CLIENT_ID` | `OAuthClient` `metadata.name` (for example `rootcoz`) |
| `SSO_OPENSHIFT_CLIENT_SECRET` | The `OAuthClient` `secret` value |
| `SSO_OPENSHIFT_ISSUER` | `https://oauth-openshift.apps.<cluster>` |
| `SSO_OPENSHIFT_API_URL` | `https://api.<cluster>:6443` |

Saving these values in Server Settings takes effect immediately with no pod
restart.

#### Trust the cluster CAs

On self-managed and default OpenShift clusters (unless the cluster API server is
configured with a publicly signed custom serving certificate, as on managed
offerings such as ROSA, OSD, or ARO), the API server's serving certificate is
signed by the cluster's own CA (never in the container's Mozilla/`certifi`
roots), so any deployment connecting to `https://api.<cluster>:6443` needs the
cluster's kube-apiserver CA in its trust store — even when the
`*.apps.<cluster>` ingress route uses a publicly signed certificate.

OpenShift SSO fails with two distinct error codes depending on which CA is
missing:

- **Untrusted OAuth route / ingress CA:** fails at `POST /oauth/token` with
  `exchange_failed` ("Could not complete the sign-in with the provider. Please
  try again.").
- **Trusted ingress CA + untrusted kube-apiserver CA:** passes `/oauth/token`
  and fails at `/apis/user.openshift.io/v1/users/~` with `userinfo_failed`
  ("Could not read your profile from the provider. Please try again.").

> **Do not point `SSL_CERT_FILE` at the cluster CA alone:** doing so replaces
> the system trust store and breaks connections to public CAs such as Google
> and GitHub. Always build a **combined** bundle starting from the container's
> immutable system CA bundle (`/etc/ssl/certs/ca-certificates.crt`) and append
> the cluster CAs to it.

1. Build `ca-bundle.crt` starting from the container's immutable system CA
   bundle (`/etc/ssl/certs/ca-certificates.crt`, which — like `/app/.venv`'s
   `certifi` bundle — carries Mozilla roots), then append the cluster
   kube-apiserver CA from the namespace's standard `kube-root-ca.crt` ConfigMap
   and the cluster ingress CA from `default-ingress-cert`. Reading
   `kube-root-ca.crt` works even when the pod sets
   `automountServiceAccountToken: false` (the Helm chart default, where
   `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` is not mounted), and
   starting from `/etc/ssl/certs/ca-certificates.crt` rather than
   `${SSL_CERT_FILE:-...}` keeps rebuilds idempotent without duplicating
   certificates or retaining revoked CAs. Building into a temporary file,
   setting mode `0644` (clearing mktemp's 0600 mode so the bundle stays
   readable wherever it is later mounted), and moving it into
   place only on success ensures a failed `oc` command never leaves a partial
   `ca-bundle.crt` for step 2:

   ```bash
   (
     set -eu
     out=$(mktemp)
     trap 'rm -f "$out"' EXIT
     oc -n <namespace> exec deployment/<deployment> -- \
       cat /etc/ssl/certs/ca-certificates.crt > "$out"
     printf '\n' >> "$out"
     oc -n <namespace> get configmap kube-root-ca.crt \
       -o jsonpath='{.data.ca\.crt}' >> "$out"
     printf '\n' >> "$out"
     oc -n openshift-config-managed get configmap default-ingress-cert \
       -o jsonpath='{.data.ca-bundle\.crt}' >> "$out"
     printf '\n' >> "$out"
     chmod 0644 "$out"
     mv "$out" ca-bundle.crt
     trap - EXIT
   )
   ```

   If your OAuth route (`https://oauth-openshift.apps.<cluster>`) already uses
   a publicly signed certificate, omit the `default-ingress-cert` command from
   the subshell; if you lack read access to `openshift-config-managed`, replace
   it inside the same subshell with `cat /path/to/ingress-ca.pem >> "$out"`
   (keeping the `printf '\n' >> "$out"` separator before `chmod 0644 "$out"`
   and `mv "$out" ca-bundle.crt`). Do the same for any custom API load-balancer
   CA (if not in `kube-root-ca.crt`) or corporate root CA, or append an extra
   PEM file to `ca-bundle.crt` before step 2 with explicit newline boundaries:

   ```bash
   (
     set -eu
     printf '\n' >> ca-bundle.crt
     cat /path/to/extra-ca.pem >> ca-bundle.crt
     printf '\n' >> ca-bundle.crt
   )
   ```

2. Save the combined bundle to a ConfigMap (`rootcoz-extra-ca`) and remove the
   local file once the apply succeeds:

   ```bash
   oc -n <namespace> create configmap rootcoz-extra-ca \
     --from-file=ca-certificates.crt=ca-bundle.crt \
     --dry-run=client -o yaml \
     | oc -n <namespace> apply --server-side --force-conflicts -f - \
     && rm -f ca-bundle.crt
   ```

   `--server-side` is used because the combined CA bundle approaches
   Kubernetes' 256 KiB `last-applied-configuration` annotation limit. Treat
   `rootcoz-extra-ca` (and `SSL_CERT_FILE`) as a security-sensitive trust store:
   restrict write RBAC and review changes, since adding an untrusted CA enables
   intercepting outbound TLS traffic (including OAuth token exchanges).

3. Mount the ConfigMap read-only at `/etc/ssl/extra` and set `SSL_CERT_FILE`.
   Because the RootCoz Deployment uses `replicas: 1` with `strategy: Recreate`,
   pause before applying both changes so only a single rollout happens, pass
   `--overwrite` to `oc set volume` so re-running this step is idempotent, then
   resume and wait for the rollout to finish before step 4:

   ```bash
   oc -n <namespace> rollout pause deployment/<deployment>
   oc -n <namespace> set volume deployment/<deployment> \
     --add --overwrite --name=extra-ca \
     --type=configmap --configmap-name=rootcoz-extra-ca \
     --mount-path=/etc/ssl/extra --read-only
   oc -n <namespace> set env deployment/<deployment> \
     SSL_CERT_FILE=/etc/ssl/extra/ca-certificates.crt
   oc -n <namespace> rollout resume deployment/<deployment>
   oc -n <namespace> rollout status deployment/<deployment>
   ```

   Once the ConfigMap is mounted and `SSL_CERT_FILE` is set, later updates to
   `rootcoz-extra-ca` refresh automatically via kubelet in roughly 30–60 seconds
   without restarting the pod. Rebuild `rootcoz-extra-ca` after upgrading the
   RootCoz image or rotating cluster CAs, because the ConfigMap snapshots the
   image's CA store rather than refreshing from it automatically.

   > **Helm-managed deployments:** The official Helm chart (see
   > [Deploying RootCoz](deploy-rootcoz.html)) does not currently expose extra CA
   > volume or `SSL_CERT_FILE` values, so `helm upgrade` will revert imperative
   > `oc set volume` and `oc set env` patches. Re-apply these patches after
   > upgrades, or inject the volume, mount, and `SSL_CERT_FILE` environment
   > variable with a Helm post-renderer or manifest overlay. For non-Kubernetes
   > deployments such as Docker Compose, extract the base bundle from the
   > RootCoz image (for example
   > `docker run --rm --entrypoint cat ghcr.io/myk-org/rootcoz:latest /etc/ssl/certs/ca-certificates.crt`),
   > append the cluster CAs, keep the combined file mode `0644` (the container
   > runs as non-root `appuser`), mount it read-only into the container, and
   > set `SSL_CERT_FILE` to its container path.

4. Verify TLS from inside the pod against the API server, the OAuth server, and
   a public HTTPS endpoint:

   ```bash
   oc -n <namespace> exec deployment/<deployment> -- /app/.venv/bin/python -c '
   import httpx
   for url in (
       "https://api.<cluster>:6443/apis/user.openshift.io/v1/users/~",
       "https://oauth-openshift.apps.<cluster>/healthz",
       "https://accounts.google.com/.well-known/openid-configuration",
   ):
       try:
           print(httpx.get(url, timeout=10).status_code, url)
       except Exception as exc:
           print("ERROR", url, exc)
   '
   ```

   Expect `403` or `401` from `users/~` (depending on whether anonymous
   authentication is enabled on the cluster — either confirms TLS verification
   succeeded against the kube-apiserver CA), `200` from the OAuth server's
   `/healthz` endpoint, and `200` from the public endpoint confirming public
   CAs remain intact.

#### Scope limitation: email and `SSO_ALLOWED_DOMAINS`

RootCoz requests only OpenShift's `user:info` OAuth scope, which OpenShift
scopes strictly to `get` on `users/~` (and not `identities`) regardless of
cluster RBAC — OpenShift intersects token scopes with RBAC, so even a
`cluster-admin` user's token receives `403` on `identities` lookups. Because no
email address is returned, setting `SSO_ALLOWED_DOMAINS` refuses OpenShift
logins with `domain_not_allowed`:

- If you need OpenShift SSO, leave `SSO_ALLOWED_DOMAINS` empty (note that it is
  deployment-wide across all enabled SSO providers, so pair it with
  `REQUIRE_APPROVAL` to gate new accounts and `ALLOWED_USERS` to bound
  registration and who may create or modify data).
- Use **Red Hat SSO** or **Google** (or **GitHub**, whose accounts must have a
  verified email address) when domain allow-listing is required.

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
deployment's IdP or cluster admin can write it); a login whose address is
missing or neither of those is refused. For OpenShift, RootCoz's `user:info`
scope cannot read `identities`, so no email is returned and OpenShift logins are
refused while `SSO_ALLOWED_DOMAINS` is set (see **Scope limitation: email and
`SSO_ALLOWED_DOMAINS`** above). It never applies to username + API key logins or
to `ADMIN_KEY`, so an administrator cannot lock themselves out with it.

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

## Related Pages

- [Deploying RootCoz](deploy-rootcoz.html)
- [Configuration Reference](configuration-reference.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)

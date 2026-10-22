# MedCare Resources for ChatGPT — setup

**Status (2026-10-21):**
- The remote server is built and tested, but **not deployed**.
- It has **not** been tested inside ChatGPT. That end-to-end test needs a hosted HTTPS URL, an identity-provider tenant and an eligible ChatGPT account, and none of these exist yet.
- Until it is deployed and tested, use the Codex plugin. It runs the same tools locally.

The ChatGPT version is the **same** MedCare Resources server: the same 28 tools, schemas, lookup logic and `medication_list_check` batch tool. The only difference is how it is reached. Codex starts it locally over stdio. ChatGPT reaches a hosted copy over HTTPS (MCP Streamable HTTP), protected by OAuth.

```
ChatGPT ──HTTPS + Bearer token──▶  remote/medcare_http.py  ──▶  plugins/medcare-resources/server.py (same tools)
   │                                    │ verifies token (JWKS), allowlist, limits
   └──── OAuth sign-in (PKCE) ───▶  Identity provider (Auth0 / Stytch / WorkOS / Okta …): your team's individual accounts
```

---

## 1. Which ChatGPT accounts can use it

According to OpenAI's help article [Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt) (retrieved 2026-10-21; it was then "updated 6 days ago"):

| Plan | What the article says | What it means for MedCare Resources (all 28 tools are read-only) |
|---|---|---|
| **Pro** | Can connect MCP servers with read/fetch permissions in **developer mode** | Should work: each person turns on developer mode and adds the server. **Not yet tested.** |
| **Business** | Full MCP (beta). "Only admins/owners can enable developer mode and deploy apps"; admins cannot enable developer mode for individual members; only admins and owners can publish apps | An admin or owner adds the server and publishes it to the workspace; members then use it. Each member still signs in with their **own** account. **Not yet tested.** |
| **Enterprise / Edu** | Full MCP (beta); admins can grant developer mode to specific members through RBAC | Like Business, with finer control. **Not yet tested.** |
| **Free / Go / Plus** | Not mentioned | **Assume it is not available.** Do not promise it to these tiers. |

Other points from the same article:
- It works **on the ChatGPT website only**. The mobile apps are not supported, and the article says nothing about the desktop apps.
- The feature is in **beta**: "Functionality, UI, and permissions may change."

Not yet verified:
- whether Shawn's personal account qualifies (which plan, and whether developer mode is available on it);
- whether members of a Business workspace can use an admin-published custom app without developer mode;
- how often ChatGPT asks people to sign in again.

These get confirmed during the first real test (§5).

## 2. How team-only access works

- Every teammate signs in with an **individual account** at your identity provider (IdP). There are no shared passwords and no shared links with secrets in them.
- ChatGPT runs the standard OAuth authorization-code flow with PKCE (S256) and receives an access token for that person. The server never sees passwords and never issues tokens.
- On **every** request the server checks:
  - the token signature, against the IdP's public keys (JWKS);
  - the issuer (`iss`);
  - the audience (`aud` must equal this server's URL, which ChatGPT sends as the `resource` parameter);
  - expiry;
  - the `medcare:read` scope.
- It then checks the person's **verified email** (or IdP subject ID) against `MEDCARE_ALLOWED_EMAILS` / `MEDCARE_ALLOWED_SUBJECTS`.
  - Not on the list gives `403`, even with a valid login.
  - No token or a bad token gives `401` with `WWW-Authenticate: Bearer resource_metadata="…"`, which is how ChatGPT discovers where to sign in.
- **Defense in depth:** also turn off public sign-up at the IdP, so only invited people can create accounts.
- **Removing someone:** delete them from the allowlist, restart the server, and block them in the IdP.

## 3. Operator setup (one time)

You need to make or confirm these decisions before deploying:

| Decision | Recommended default | Notes |
|---|---|---|
| **Hosting account** | Fly.io (`remote/fly.toml.example`) or Render (`remote/render.yaml.example`) | Any host that runs a Docker container with HTTPS and a small disk works. **Not** Wayfinder infrastructure. |
| **Public URL / domain** | Start with the platform URL (for example `https://medcare-resources-mcp.fly.dev/mcp`); add a custom subdomain later if you want one | `MEDCARE_RESOURCE_URL` must be exactly this URL. |
| **Identity provider** | Auth0 (free tier) | Stytch and WorkOS also support MCP/OAuth. Okta and Entra work if they issue JWT access tokens for a custom API. |
| **Allowlist** | The team's work emails | Kept in host settings, not in the repo. |
| **ChatGPT account(s) for the first test** | One eligible account (Pro, or a Business/Enterprise admin) | Needed before anyone can claim ChatGPT works. |

### 3a. Identity provider (Auth0 example)
Setting names below come from Auth0's documentation. Confirm them in your dashboard; Auth0 changes its UI.

1. **Create the tenant.** Under **Settings → Advanced**, turn on:
   - **Resource Parameter Compatibility Profile**, so ChatGPT's `resource` becomes the token audience ([Auth0 doc](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile));
   - **Include Issuer in Authorization Responses**. OpenAI requires RFC 9207 `iss`.
2. **Create the API.** Go to **Applications → APIs → Create API**:
   - Identifier: your exact `MEDCARE_RESOURCE_URL` (for example `https://medcare-resources-mcp.fly.dev/mcp`).
   - Signing algorithm: RS256.
   - Turn on **RBAC** and **Add Permissions in the Access Token**, then add the permission `medcare:read`.
   - Turn on **Allow Offline Access**. OpenAI says refresh tokens (`offline_access`) keep the connection alive.
3. **Create the application ChatGPT will use.** The simplest option is a predefined client: a **Regular Web Application**.
   - Allowed Callback URL: the redirect URI shown on ChatGPT's connector settings page. Per OpenAI this is `https://chatgpt.com/connector_platform_oauth_redirect`, or `https://chatgpt.com/connector/oauth/{callback_id}`. Copy the exact value ChatGPT shows.
   - Grant types: Authorization Code and Refresh Token.
   - Then give every teammate who should have access the `medcare:read` permission: through a role (**User Management → Roles**), or by authorising the application's access to the API.
   - Alternative: Dynamic Client Registration or CIMD, if your IdP supports them. OpenAI prefers CIMD, but a predefined client is also supported.
4. **Users.**
   - Turn off sign-ups on the database connection and invite each teammate.
   - Or use Google Workspace or Microsoft login restricted to your domain.
   - Either way, the server's allowlist still applies.
5. **Email claim.** Add a post-login Action so access tokens carry the verified email. (Without it, the server falls back to the IdP's `/userinfo`, which also works.)
   - The server accepts an email only when its verified flag is the JSON value `true`. Strings such as `"true"`, `1` or a missing flag are refused.
   - A `/userinfo` answer counts only if it names exactly the same `sub` as the token.
   - The token is sent to `/userinfo` only over HTTPS on the issuer's host (or a host you configure explicitly in `MEDCARE_OAUTH_USERINFO_URL`). Redirects are never followed.
   ```js
   exports.onExecutePostLogin = async (event, api) => {
     if (event.user.email && event.user.email_verified) {
       api.accessToken.setCustomClaim("https://medcare-resources/email", event.user.email);
       api.accessToken.setCustomClaim("https://medcare-resources/email_verified", true);
     }
   };
   ```

### 3b. Deploy the server
1. Copy `remote/.env.example` into your host's environment settings and fill in:
   - `MEDCARE_RESOURCE_URL`
   - `MEDCARE_OAUTH_ISSUER` (Auth0: `https://<tenant>.<region>.auth0.com/`, with the trailing slash)
   - `MEDCARE_ALLOWED_EMAILS`

   There are **no secrets** in the server configuration: it verifies tokens with public keys. The only secret, the OAuth client secret, is entered into ChatGPT's connector settings. Never put it in a URL, the repo or the server.
2. Build and run from the repository root:
   - Fly: `fly launch --copy-config` with `remote/fly.toml.example` copied to `fly.toml`, then `fly volumes create medcare_cache --size 1`, then `fly deploy`.
   - Render: use `remote/render.yaml.example` as a blueprint.
   - Docker anywhere: `docker build -f remote/Dockerfile -t medcare-resources-remote .`, then run it with a volume at `/data` and the environment set.
3. Check the deployment:
   - `curl https://<host>/healthz` returns `{"status":"ok", "tools":28, …}`.
   - `curl -i -X POST https://<host>/mcp -H 'Content-Type: application/json' -d '{}'` returns `401` with a `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"` header.
   - `curl https://<host>/.well-known/oauth-protected-resource` shows your resource URL and issuer.
   - Optional: connect with [MCP Inspector](https://github.com/modelcontextprotocol/inspector) and sign in as an allowlisted user.

### 3c. Add it to ChatGPT (admin or Pro user)
This follows OpenAI's [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt) and [Quickstart](https://developers.openai.com/plugins/quickstart) pages.

1. Go to <https://chatgpt.com/plugins>, click **+**, then **Add custom MCP server**. (On Pro, turn on developer mode first. On Business or Enterprise, an admin enables it under Workspace Settings → Permissions & Roles → Connected Data.)
2. Name: **MedCare Resources**. Description: *Public MedCare Partners (H5767) and Medicare plan research: plan documents, drug lists, providers, pharmacies, CMS data.*
3. Connection: enter the server URL, `https://<host>/mcp`.
4. Authentication: **OAuth**. If you made a predefined client, enter its client ID and secret here. Review the warning, choose **I understand and want to continue**, then **Create as a plugin**.
5. Check that ChatGPT lists the 28 tools. To publish to a Business or Enterprise workspace, an admin or owner publishes it.

## 4. Teammate instructions (short version)

1. Use ChatGPT **on the web** with an eligible account (your admin will tell you which).
2. If MedCare Resources was published to your workspace, go to **Plugins** and install it. On a Pro account, add it yourself with steps 3c-1 to 3c-4, using the URL your admin gives you.
3. The first time, ChatGPT asks you to **sign in**. Use your own team account. If you get "not on the allowlist", ask the admin to add your email.
4. Start a new chat, type **@**, and choose **MedCare Resources**.
5. **Never paste names, dates of birth, Medicare/member IDs, SSNs or notes.** Paste only drug names, strengths and directions. Identifier-looking lines are removed automatically, but don't rely on that.

**Sample full-list prompt** (synthetic list):
```
@MedCare Resources Check this whole medication list against MedCare Classic, Focus and Choice for 2027.
Show the full table, then list anything that needs clarification or failed lookup.

1. Lisinopril 10 mg tablet daily
2) Metformin ER 500mg - take 1 tab twice daily with meals
- Eliquis 5 mg BID
• atorvastatin 40 mg, simvastatin 20 mg
* Losartan-HCTZ 50-12.5 mg daily
Levothyroxine 25 mcg; omeprazole 20 mg DR capsule
Lantus SoloStar 100 units/mL pen, inject 20 units at bedtime
amlodipine 5mg | Toprol XL 50 mg
hydrocodone/acetaminophen 5/325 mg q6h prn
Ozempic 1 mg weekly
gabapentin 300 mg capsule
furosemide 40 mg
```
To compare with a competitor, add *"also compare plan H7993-030 for 2027"*. Any Part D plan ID works, up to 6 plans per call.

What to expect:
- one row per entry, in your order, with the exact product matched and, for each plan, the tier, PA/ST/QL and cost sharing;
- flags for duplicates, entries that need a strength or form, and brand/generic differences;
- `lookup_failed` means *unknown*, never "not covered".

## 5. First real ChatGPT test (still to do)
Once the server is deployed:
1. Connect from one eligible account.
2. Confirm the sign-in, that a non-allowlisted account is refused, tools/list, `source_status`, and the sample prompt above.
3. Confirm how long ChatGPT waits for a long list. The server defaults to a 90-second budget per call, and anything unfinished is reported per item.
4. Record the plan and results in CHATGPT_BUILD_REPORT.md.

Only then say that it works in ChatGPT.

## 6. Operations
- **Size:**
  - one small instance (shared CPU, 512 MB–1 GB RAM; measured peak about 150 MB with an empty cache);
  - a 1 GB volume (the CMS cache is about 110–160 MB);
  - HTTPS from the platform.
- **Cost:**
  - typically about $5–15 a month for the host, depending on the provider and plan;
  - $0 for the IdP free tier at team size (check the provider's current limits);
  - no other paid services.
- **Limits (environment variables):**
  - 4 concurrent tool calls, with a 20 s queue and then `503` plus `Retry-After`;
  - 30 requests a minute per user (burst 15), then `429`;
  - 256 KB request body;
  - 300 s request timeout;
  - a default 90 s budget for medication lists.
- **Health:** `GET /healthz` (no auth, no data). Container `HEALTHCHECK` and platform checks are included.
- **Logs:** only method, status, duration and a short hash of the user ID. No request bodies, medication lists, emails or tokens. Tracebacks are off (`MEDCARE_LOG_TRACEBACKS=0`).
- **Data:**
  - Public data only. CMS files are cached on the volume.
  - Medication lists are processed in memory and not stored. The RxNorm lookup cache holds single drug names only, is bounded, and is kept in memory.
  - The server sends only drug names (to NLM RxNav) and plan IDs to public sources.
- **Updates:** redeploy from a reviewed commit SHA. In ChatGPT, open the connection under Plugins and choose **Refresh** to pick up tool changes.
- **Private alternative:** OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) can reach a server that is not on the public internet. It needs an OpenAI Platform organization with tunnel roles and a running `tunnel-client`, and has a 120 s request deadline. The IdP must still be reachable.

# Here's a recap of the debugging session — you were fixing SSO/logout for `home.palashkantikundu.in/ai`:

**1. Initial 404 on `/ai/`**

- Root cause: `local-ai-authentik-outpost-1` was `unhealthy`, so `auth_request` calls to it failed and the login redirect chain dead-ended in a 404.

**2. Outpost `unhealthy`, config had a typo**

- `authentik-compose.yaml` had `UTHENTIK_TOKEN` (missing the leading `A`) instead of `AUTHENTIK_TOKEN` — the env var was silently ignored.

**3. `502 Bad Gateway` fetching outpost config**

- After fixing the typo, the outpost was trying to reach Authentik via the *public* URL (`https://home.palashkantikundu.in/sso/`), round-tripping out to the internet unnecessarily. Fixed by pointing `AUTHENTIK_HOST` at the internal Docker service name instead, and adding `AUTHENTIK_HOST_BROWSER` for user-facing redirects.

**4. `404 Not Found` fetching outpost config**

- `AUTHENTIK_HOST` was missing the `/sso/` path suffix that `AUTHENTIK_WEB__PATH: /sso/` requires on the server side. Fixed to `http://authentik-server:9000/sso/`.

**5. OAuth "Redirect URI Error"**

- The Proxy Provider's **External Host** field in the Authentik admin UI was pointing at an internal address instead of `https://home.palashkantikundu.in`. Fixed in the admin UI, then restarted the outpost to re-sync.

**6. 502 on the app itself**

- Traced to checking whether `chat-webui.py` (port 3001) was actually running — this resolved once confirmed/started.

**7. Login worked. Logout redirected to `/sso/outpost.goauthentik.io/end?rd=/` → 404**

- Found the bug in `src/api.js`: the logout URL was hardcoded with an incorrect `/sso/` prefix. Nginx only routes `/outpost.goauthentik.io/*` at the domain root, not under `/sso/`. Fixed the JS to `window.location.assign('/outpost.goauthentik.io/end?rd=/')` and rebuilt the frontend.

**8. Still 404 on `/outpost.goauthentik.io/end?rd=/` — unresolved**

- Confirmed via nginx access log that nginx is routing this correctly (the 404 comes from the outpost itself, not nginx).
- Confirmed via direct `curl` to `127.0.0.1:9010` (bypassing nginx) that the outpost returns a bare `404 page not found` for this path.
- Current working theory: the `/end` logout endpoint needs the browser's Authentik session cookie to identify which provider/session to terminate, and it may not be present/forwarded on that request. **Next steps left open:** check dev tools for the `authentik_proxy_*` cookie on that request, and retest the curl call with that cookie attached to confirm.

That last piece (logout 404) is where we left off — still open.
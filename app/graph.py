"""The little bit of Microsoft Graph that rclone cannot do for us.

rclone moves the bytes. It cannot, headlessly, turn a SharePoint site URL into
the drive id it needs — its interactive config does that by asking questions. So
this module does three things and nothing else: get an app-only token, resolve a
library, and answer "are these credentials still good?".

App-only (client credentials) throughout: there is no user in the loop, which is
what lets an unattended sync work and what lets an administrator finish setup in
a form rather than a browser redirect. The corresponding Entra permission is an
*application* permission — `Sites.Selected`, granted on the one library, is the
right one; `Sites.Read.All` would hand over every site in the tenant.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import httpx

LOGIN_HOST = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class GraphError(RuntimeError):
    """Anything that went wrong talking to Microsoft, with a usable message."""


def _explain(exc: httpx.HTTPStatusError) -> str:
    """Microsoft's errors are informative; surface them instead of the status."""
    try:
        body = exc.response.json()
    except ValueError:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    err = body.get("error")
    if isinstance(err, dict):  # Graph
        return f"{err.get('code', exc.response.status_code)}: {err.get('message', '')}"[:400]
    if isinstance(err, str):  # the token endpoint
        detail = body.get("error_description", "").split("\r\n")[0]
        return f"{err}: {detail}"[:400]
    return f"HTTP {exc.response.status_code}"


async def token(tenant: str, client_id: str, client_secret: str) -> str:
    """An app-only access token. Raises GraphError with Microsoft's own wording."""
    url = f"{LOGIN_HOST}/{tenant}/oauth2/v2.0/token"
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.post(url, data=form)
            res.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GraphError(_explain(exc)) from exc
        except httpx.HTTPError as exc:
            raise GraphError(f"cannot reach {LOGIN_HOST}: {exc}") from exc
    access = res.json().get("access_token")
    if not access:
        raise GraphError("the token response contained no access_token")
    return access


def split_site_url(site_url: str) -> tuple[str, str]:
    """Split a SharePoint site URL into (hostname, server-relative path).

    Accepts what someone actually pastes out of the browser — including a URL
    pointing at a document library or a folder inside one:

        https://contoso.sharepoint.com/sites/ProjectX            -> (host, "/sites/ProjectX")
        https://contoso.sharepoint.com/sites/ProjectX/Shared%20Documents/DD
                                                                 -> (host, "/sites/ProjectX")
    """
    parsed = urlparse(site_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise GraphError(f"not a URL: {site_url!r}")
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    # A site is /sites/<name> or /teams/<name>; anything deeper is inside a
    # library, and the library is selected separately.
    if len(parts) >= 2 and parts[0].lower() in {"sites", "teams"}:
        path = f"/{parts[0]}/{parts[1]}"
    elif parts:
        path = f"/{parts[0]}"
    else:
        path = ""  # the tenant root site
    return parsed.netloc, path


async def _get(client: httpx.AsyncClient, url: str, access: str) -> dict:
    try:
        res = await client.get(url, headers={"Authorization": f"Bearer {access}"})
        res.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GraphError(_explain(exc)) from exc
    except httpx.HTTPError as exc:
        raise GraphError(f"cannot reach Graph: {exc}") from exc
    return res.json()


async def resolve_library(access: str, site_url: str, library: str | None = None) -> dict:
    """Find the drive (document library) behind a site URL.

    Returns {site_id, site_name, drive_id, drive_name, web_url, available}. When
    `library` is given it must match a library name on that site; otherwise the
    first document library is used — which for a normal team site is the one
    called "Documents"/"Shared Documents".
    """
    host, path = split_site_url(site_url)
    site_ref = f"{host}:{path}:" if path else host
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        site = await _get(client, f"{GRAPH}/sites/{site_ref}", access)
        site_id = site.get("id")
        if not site_id:
            raise GraphError(f"no site at {site_url}")
        drives = (await _get(client, f"{GRAPH}/sites/{site_id}/drives", access)).get("value", [])

    if not drives:
        raise GraphError(
            "the site has no document libraries visible to this app — if you used "
            "Sites.Selected, check the permission was granted on this site"
        )
    available = [d.get("name") for d in drives if d.get("name")]
    if library:
        wanted = library.strip().lower()
        chosen = next((d for d in drives if (d.get("name") or "").lower() == wanted), None)
        if chosen is None:
            raise GraphError(f"no library named {library!r}. Available: {', '.join(available)}")
    else:
        chosen = next((d for d in drives if d.get("driveType") == "documentLibrary"), drives[0])
    return {
        "site_id": site_id,
        "site_name": site.get("displayName") or site.get("name"),
        "drive_id": chosen["id"],
        "drive_name": chosen.get("name"),
        "web_url": chosen.get("webUrl"),
        "available": available,
    }


async def probe(
    tenant: str, client_id: str, client_secret: str, site_url: str,
    library: str | None = None, drive_id: str | None = None,
) -> dict:
    """Cheapest useful liveness check: a token, and one look at the drive root.

    Never raises — returns {"ok": bool, "note": str, ...} so the caller can store
    the verdict the way API-key testing does.
    """
    try:
        access = await token(tenant, client_id, client_secret)
    except GraphError as exc:
        return {"ok": False, "note": f"sign-in failed — {exc}"}

    try:
        if drive_id:
            resolved = {"drive_id": drive_id, "drive_name": None}
        else:
            resolved = await resolve_library(access, site_url, library)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            root = await _get(
                client,
                f"{GRAPH}/drives/{resolved['drive_id']}/root?$select=id,name,size,folder",
                access,
            )
    except GraphError as exc:
        return {"ok": False, "note": str(exc)}

    child_count = (root.get("folder") or {}).get("childCount")
    size = root.get("size")
    note = f"reached {resolved.get('drive_name') or 'the library'}"
    if child_count is not None:
        note += f" · {child_count} item(s) at the root"
    if size:
        note += f" · {size / 1e9:.2f} GB"
    return {
        "ok": True,
        "note": note,
        "drive_id": resolved["drive_id"],
        "drive_name": resolved.get("drive_name"),
        "site_id": resolved.get("site_id"),
        "web_url": resolved.get("web_url"),
        "bytes_total": size or 0,
    }

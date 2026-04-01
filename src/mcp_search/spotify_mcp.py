"""MCP server for Spotify — uses spotify-token-proxy for auth, calls Spotify Web API directly."""

import json
import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_SPOTIFY_API = "https://api.spotify.com/v1"
_TOKEN_PROXY = os.environ.get("SPOTIFY_TOKEN_PROXY_URL", "http://172.24.0.1:8095")


@lifespan
async def spotify_lifespan(server):
    client = httpx.AsyncClient(timeout=15.0)
    yield {"client": client}
    await client.aclose()


mcp = FastMCP("spotify", lifespan=spotify_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


async def _get_token() -> str:
    resp = await _client().get(f"{_TOKEN_PROXY}/token/user")
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _headers() -> dict:
    return {"Authorization": f"Bearer {await _get_token()}"}


async def _spotify_get(path: str, params: dict | None = None) -> dict:
    resp = await _client().get(f"{_SPOTIFY_API}{path}", headers=await _headers(), params=params)
    resp.raise_for_status()
    return resp.json()


async def _spotify_put(path: str, json_body: dict | None = None) -> dict | None:
    resp = await _client().put(f"{_SPOTIFY_API}{path}", headers=await _headers(), json=json_body)
    resp.raise_for_status()
    return resp.json() if resp.content else None


async def _spotify_post(path: str, json_body: dict | None = None, params: dict | None = None) -> dict | None:
    resp = await _client().post(f"{_SPOTIFY_API}{path}", headers=await _headers(), json=json_body, params=params)
    resp.raise_for_status()
    return resp.json() if resp.content else None


async def _spotify_delete(path: str, json_body: dict | None = None) -> dict | None:
    resp = await _client().request("DELETE", f"{_SPOTIFY_API}{path}", headers=await _headers(), json=json_body)
    resp.raise_for_status()
    return resp.json() if resp.content else None


# --- Response formatters ---

def _fmt_track(t: dict, detailed: bool = False) -> dict:
    r = {"name": t["name"], "id": t["id"]}
    artists = [a["name"] for a in t.get("artists", [])]
    r["artist"] = artists[0] if len(artists) == 1 else artists
    if detailed:
        r["album"] = t.get("album", {}).get("name")
        r["duration_ms"] = t.get("duration_ms")
        r["track_number"] = t.get("track_number")
    if "is_playing" in t:
        r["is_playing"] = t["is_playing"]
    return r


def _fmt_album(a: dict, detailed: bool = False) -> dict:
    r = {"name": a["name"], "id": a["id"]}
    artists = [ar["name"] for ar in a.get("artists", [])]
    r["artist"] = artists[0] if len(artists) == 1 else artists
    if detailed:
        r["release_date"] = a.get("release_date")
        r["total_tracks"] = a.get("total_tracks")
        if "tracks" in a:
            r["tracks"] = [_fmt_track(t) for t in a["tracks"].get("items", [])]
    return r


def _fmt_artist(a: dict, detailed: bool = False) -> dict:
    r = {"name": a["name"], "id": a["id"]}
    if detailed:
        r["genres"] = a.get("genres", [])
        r["popularity"] = a.get("popularity")
    return r


def _fmt_playlist(p: dict, detailed: bool = False) -> dict:
    r = {"name": p["name"], "id": p["id"], "owner": p.get("owner", {}).get("display_name")}
    if detailed:
        r["description"] = p.get("description")
        if "tracks" in p:
            r["tracks"] = [_fmt_track(t["track"]) for t in p["tracks"].get("items", []) if t.get("track")]
    return r


# --- Tools ---

@mcp.tool()
async def spotify_playback(action: str, track_id: str | None = None, num_skips: int = 1) -> str:
    """Control Spotify playback.

    Args:
        action: One of 'get', 'start', 'pause', 'skip'.
        track_id: Spotify track ID to play (for 'start' action). Omit to resume.
        num_skips: Number of tracks to skip (for 'skip' action).
    """
    match action:
        case "get":
            data = await _spotify_get("/me/player/currently-playing")
            if not data or not data.get("item"):
                return "No track currently playing."
            track = _fmt_track(data["item"])
            track["is_playing"] = data.get("is_playing", False)
            return json.dumps(track, indent=2)
        case "start":
            body = {"uris": [f"spotify:track:{track_id}"]} if track_id else None
            await _spotify_put("/me/player/play", json_body=body)
            return f"Playback started{f' for track {track_id}' if track_id else ''}."
        case "pause":
            await _spotify_put("/me/player/pause")
            return "Playback paused."
        case "skip":
            for _ in range(num_skips):
                await _spotify_post("/me/player/next")
            return f"Skipped {num_skips} track(s)."
        case _:
            return f"Unknown action: {action}. Use 'get', 'start', 'pause', or 'skip'."


@mcp.tool()
async def spotify_search(query: str, qtype: str = "track", limit: int = 10) -> str:
    """Search Spotify for tracks, albums, artists, or playlists.

    Args:
        query: Search query.
        qtype: Type of items: 'track', 'album', 'artist', 'playlist', or comma-separated.
        limit: Max results per type.
    """
    data = await _spotify_get("/search", params={"q": query, "type": qtype, "limit": limit})
    results = {}
    for qt in qtype.split(","):
        qt = qt.strip()
        key = f"{qt}s"
        if key in data:
            match qt:
                case "track":
                    results["tracks"] = [_fmt_track(t) for t in data[key].get("items", []) if t]
                case "album":
                    results["albums"] = [_fmt_album(a) for a in data[key].get("items", []) if a]
                case "artist":
                    results["artists"] = [_fmt_artist(a) for a in data[key].get("items", []) if a]
                case "playlist":
                    results["playlists"] = [_fmt_playlist(p) for p in data[key].get("items", []) if p]
    return json.dumps(results, indent=2)


@mcp.tool()
async def spotify_queue(action: str, track_id: str | None = None) -> str:
    """Manage the Spotify playback queue.

    Args:
        action: 'get' to view queue, 'add' to add a track.
        track_id: Spotify track ID (required for 'add').
    """
    match action:
        case "get":
            data = await _spotify_get("/me/player/queue")
            current = _fmt_track(data["currently_playing"]) if data.get("currently_playing") else None
            queue = [_fmt_track(t) for t in data.get("queue", [])]
            return json.dumps({"currently_playing": current, "queue": queue}, indent=2)
        case "add":
            if not track_id:
                return "track_id is required for 'add' action."
            await _spotify_post("/me/player/queue", params={"uri": f"spotify:track:{track_id}"})
            return "Track added to queue."
        case _:
            return f"Unknown action: {action}. Use 'get' or 'add'."


@mcp.tool()
async def spotify_get_info(item_id: str, qtype: str = "track") -> str:
    """Get detailed info about a Spotify item.

    Args:
        item_id: Spotify ID of the item.
        qtype: Type: 'track', 'album', 'artist', or 'playlist'.
    """
    match qtype:
        case "track":
            data = await _spotify_get(f"/tracks/{item_id}")
            return json.dumps(_fmt_track(data, detailed=True), indent=2)
        case "album":
            data = await _spotify_get(f"/albums/{item_id}")
            return json.dumps(_fmt_album(data, detailed=True), indent=2)
        case "artist":
            data = await _spotify_get(f"/artists/{item_id}")
            info = _fmt_artist(data, detailed=True)
            top = await _spotify_get(f"/artists/{item_id}/top-tracks")
            info["top_tracks"] = [_fmt_track(t) for t in top.get("tracks", [])]
            albums = await _spotify_get(f"/artists/{item_id}/albums", params={"limit": 20})
            info["albums"] = [_fmt_album(a) for a in albums.get("items", [])]
            return json.dumps(info, indent=2)
        case "playlist":
            data = await _spotify_get(f"/playlists/{item_id}")
            return json.dumps(_fmt_playlist(data, detailed=True), indent=2)
        case _:
            return f"Unknown type: {qtype}. Use 'track', 'album', 'artist', or 'playlist'."


@mcp.tool()
async def spotify_playlist(
    action: str,
    playlist_id: str | None = None,
    track_ids: list[str] | None = None,
    name: str | None = None,
    description: str | None = None,
    public: bool | None = None,
    limit: int = 20,
) -> str:
    """Manage Spotify playlists.

    Args:
        action: One of 'list', 'create', 'add_tracks', 'remove_tracks', 'edit'.
        playlist_id: Playlist ID (required for add_tracks, remove_tracks, edit).
        track_ids: List of Spotify track IDs (for add_tracks, remove_tracks).
        name: Playlist name (required for create, optional for edit).
        description: Playlist description (for create, edit).
        public: Whether playlist is public (for create, edit). Default None (private for create, unchanged for edit).
        limit: Max playlists to return for 'list' action.
    """
    match action:
        case "list":
            data = await _spotify_get("/me/playlists", params={"limit": limit})
            playlists = [_fmt_playlist(p) for p in data.get("items", []) if p]
            return json.dumps(playlists, indent=2)

        case "create":
            if not name:
                return "name is required for 'create' action."
            me = await _spotify_get("/me")
            body: dict = {"name": name, "public": public if public is not None else False}
            if description:
                body["description"] = description
            data = await _spotify_post(f"/users/{me['id']}/playlists", json_body=body)
            return json.dumps(_fmt_playlist(data), indent=2)

        case "add_tracks":
            if not playlist_id:
                return "playlist_id is required for 'add_tracks' action."
            if not track_ids:
                return "track_ids is required for 'add_tracks' action."
            uris = [f"spotify:track:{tid}" for tid in track_ids]
            await _spotify_post(f"/playlists/{playlist_id}/tracks", json_body={"uris": uris})
            return f"Added {len(track_ids)} track(s) to playlist."

        case "remove_tracks":
            if not playlist_id:
                return "playlist_id is required for 'remove_tracks' action."
            if not track_ids:
                return "track_ids is required for 'remove_tracks' action."
            tracks = [{"uri": f"spotify:track:{tid}"} for tid in track_ids]
            await _spotify_delete(f"/playlists/{playlist_id}/tracks", json_body={"tracks": tracks})
            return f"Removed {len(track_ids)} track(s) from playlist."

        case "edit":
            if not playlist_id:
                return "playlist_id is required for 'edit' action."
            body = {}
            if name is not None:
                body["name"] = name
            if description is not None:
                body["description"] = description
            if public is not None:
                body["public"] = public
            await _spotify_put(f"/playlists/{playlist_id}", json_body=body)
            return "Playlist updated."

        case _:
            return f"Unknown action: {action}. Use 'list', 'create', 'add_tracks', 'remove_tracks', or 'edit'."


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

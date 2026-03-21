"""Sync scrobbles from Maloja SQLite to PostgreSQL."""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

import asyncpg

MALOJA_DB = os.environ.get("MALOJA_DB", "/maloja/malojadb.sqlite")
BATCH_SIZE = 1000

SQLITE_QUERY = """
SELECT
    s.timestamp,
    s.duration,
    s.origin,
    t.title,
    t.length AS track_length,
    a.albtitle AS album_title,
    GROUP_CONCAT(ar.name, '|||') AS artists
FROM scrobbles s
JOIN tracks t ON s.track_id = t.id
LEFT JOIN albums a ON t.album_id = a.id
LEFT JOIN trackartists ta ON t.id = ta.track_id
LEFT JOIN artists ar ON ta.artist_id = ar.id
WHERE s.timestamp > ?
GROUP BY s.timestamp, s.track_id
ORDER BY s.timestamp
"""


async def sync():
    # Connect to postgres
    pg = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "scrobble"),
        password=os.environ["POSTGRES_PASSWORD"],
        database="scrobble",
    )

    try:
        # Get high-water mark
        last_ts = await pg.fetchval(
            "SELECT EXTRACT(EPOCH FROM MAX(listened_at))::bigint FROM scrobble"
        ) or 0
        print(f"High-water mark: {last_ts} ({datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts else 'none'})")

        # Read from SQLite
        sdb = sqlite3.connect(f"file:{MALOJA_DB}?mode=ro", uri=True)
        sdb.row_factory = sqlite3.Row
        rows = sdb.execute(SQLITE_QUERY, (last_ts,)).fetchall()
        sdb.close()

        if not rows:
            print("No new scrobbles to sync")
            return

        print(f"Found {len(rows)} new scrobbles to sync")

        # Collect unique artists and tracks
        artist_set: set[str] = set()
        track_set: set[tuple[str, str | None, int | None]] = set()  # (title, album, length)
        for row in rows:
            if row["artists"]:
                for name in row["artists"].split("|||"):
                    artist_set.add(name.strip())
            track_set.add((row["title"], row["album_title"], row["track_length"]))

        # Upsert artists
        print(f"Upserting {len(artist_set)} artists...")
        artist_ids: dict[str, int] = {}
        for name in artist_set:
            aid = await pg.fetchval(
                """INSERT INTO artist (name, name_lower)
                   VALUES ($1, $2)
                   ON CONFLICT (name_lower) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                name, name.lower(),
            )
            artist_ids[name] = aid

        # Upsert tracks
        print(f"Upserting {len(track_set)} tracks...")
        track_ids: dict[tuple[str, str | None], int] = {}
        for title, album_title, length in track_set:
            tid = await pg.fetchval(
                """INSERT INTO track (title, title_lower, album_title, length_secs)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (title_lower, album_title) DO UPDATE SET title = EXCLUDED.title
                   RETURNING id""",
                title, title.lower(), album_title, length,
            )
            track_ids[(title.lower(), album_title)] = tid

        # Insert scrobbles and track_artist links
        scrobble_count = 0
        for row in rows:
            title_lower = row["title"].lower()
            album_title = row["album_title"]
            tid = track_ids[(title_lower, album_title)]

            listened_at = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc)

            # Insert scrobble
            result = await pg.execute(
                """INSERT INTO scrobble (listened_at, track_id, duration, origin)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT DO NOTHING""",
                listened_at, tid, row["duration"], row["origin"],
            )

            # Insert track_artist links
            if row["artists"]:
                for name in row["artists"].split("|||"):
                    name = name.strip()
                    aid = artist_ids[name]
                    await pg.execute(
                        """INSERT INTO track_artist (track_id, artist_id)
                           VALUES ($1, $2)
                           ON CONFLICT DO NOTHING""",
                        tid, aid,
                    )

            scrobble_count += 1
            if scrobble_count % BATCH_SIZE == 0:
                print(f"  Processed {scrobble_count}/{len(rows)}")

        print(f"Sync complete: {scrobble_count} scrobbles processed")

    finally:
        await pg.close()


def main():
    asyncio.run(sync())


if __name__ == "__main__":
    main()

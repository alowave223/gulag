import asyncio

import aiomysql
import uvloop

from cmyui.mysql import AsyncSQLPool

import config

IGNORED_BEATMAP_CHARS = dict.fromkeys(map(ord, r':\/*<>?"|'), None)

async def main():
    db = AsyncSQLPool()
    await db.connect(config.mysql)

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as update_cursor:
            async with conn.cursor(aiomysql.DictCursor) as select_cursor:
                await select_cursor.execute(
                    'SELECT id, artist, title, creator, version '
                    'FROM maps WHERE filename = ""'
                )

                async for row in select_cursor:
                    map_id = row['id']
                    filename = (
                        '{artist} - {title} ({creator}) [{version}].osu'
                    ).format(**row).translate(IGNORED_BEATMAP_CHARS)

                    await update_cursor.execute(
                        'UPDATE maps SET filename = %s '
                        'WHERE id = %s',
                        [filename, map_id]
                    )

                    print(f'Updated {map_id} to {filename}')

uvloop.install()
asyncio.run(main())
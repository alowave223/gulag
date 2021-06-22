# -*- coding: utf-8 -*-

import copy
import hashlib
import random
import re
import secrets
import string
import struct
import time
import datetime
from collections import defaultdict
from enum import IntEnum
from enum import unique
from functools import wraps
from pathlib import Path
from typing import Callable
from typing import Optional
from typing import TYPE_CHECKING
from urllib.parse import unquote
from utils.recalculator import PPCalculator
from circleparse import parse_replay_file

import aiomysql
import bcrypt
import orjson
import json
from cmyui import _isdecimal
from cmyui import Ansi
from cmyui import Connection
from cmyui import Domain
from cmyui import log
from cmyui import ratelimit
from cmyui.discord import Webhook
from cmyui.discord import Embed

import packets
import utils.misc
from constants import regexes
from constants.clientflags import ClientFlags
from constants.gamemodes import GameMode
from constants.mods import Mods
from objects import glob
from objects.beatmap import Beatmap
from objects.beatmap import RankedStatus
from objects.player import Privileges
from objects.score import Score
from objects.score import SubmissionStatus
from utils.misc import escape_enum
from utils.misc import pymysql_encode
from utils.recalculator import PPCalculator

if TYPE_CHECKING:
    from objects.player import Player

""" osu: handle connections from web, api, and beyond? """

BASE_DOMAIN = glob.config.domain
domain = Domain(f'osu.{BASE_DOMAIN}')

REPLAYS_PATH = Path.cwd() / '.data/osr'
BEATMAPS_PATH = Path.cwd() / '.data/osu'
SCREENSHOTS_PATH = Path.cwd() / '.data/ss'
AVATARS_PATH = Path.cwd() / '.data/avatars'

""" Some helper decorators (used for /web/ connections) """

def _required_args(req_args: set[str], argset: str) -> Callable:
    """Decorator to ensure all required arguments are present."""
    # NOTE: this function is not meant to be used directly, but
    # rather used in the form as the functions below.
    def wrapper(f: Callable) -> Callable:

        # modify the handler code to ensure that
        # all arguments are sent in the request.
        @wraps(f)
        async def handler(conn: Connection) -> Optional[bytes]:
            args = getattr(conn, argset)

            if args.keys() >= req_args:
                # all args given, call the
                # handler with the conn.
                return await f(conn)

        return handler
    return wrapper

# the decorator above may be used
# for either args, mpargs, or files.
def required_args(req_args: set[str]) -> Callable:
    return _required_args(req_args, argset='args')
def required_mpargs(req_args: set[str]) -> Callable:
    return _required_args(req_args, argset='multipart_args')
def required_files(req_args: set[str]) -> Callable:
    return _required_args(req_args, argset='files')


def get_login(name_p: str, pass_p: str, auth_error: bytes = b'') -> Callable:
    """Decorator to ensure a player's login information is correct."""
    # NOTE: this function does NOT verify whether the arguments have
    # been passed into the connection, and assumes you have already
    # called the appropriate decorator above, @required_x.
    def wrapper(f: Callable) -> Callable:

        # modify the handler code to get the player
        # object before calling the handler itself.
        @wraps(f)
        async def handler(conn: Connection) -> Optional[bytes]:
            # args may be provided in regular args
            # or multipart, but only one at a time.
            argset = conn.args or conn.multipart_args

            if not (
                p := await glob.players.get_login(
                    name=unquote(argset[name_p]),
                    pw_md5=argset[pass_p]
                )
            ):
                # player login incorrect
                return auth_error

            # login verified, call the handler
            return await f(p, conn)
        return handler
    return wrapper

def acquire_db_conn(cursor_cls = aiomysql.Cursor) -> Callable:
    """Decorator to acquire a single database
       connection & cursor for a handler."""
    def wrapper(f: Callable) -> Callable:
        @wraps(f)
        async def handler(*args) -> Optional[bytes]:
            async with glob.db.pool.acquire() as conn:
                async with conn.cursor(cursor_cls) as db_cursor:
                    return await f(*args, db_cursor)
        return handler
    return wrapper

""" /web/ handlers """

# TODO
# POST /web/osu-session.php
# POST /web/osu-osz2-bmsubmit-post.php
# POST /web/osu-osz2-bmsubmit-upload.php
# GET /web/osu-osz2-bmsubmit-getid.php
# GET /web/osu-get-beatmap-topic.php

@domain.route('/web/osu-error.php', methods=['POST'])
async def osuError(conn: Connection) -> Optional[bytes]:
    if glob.app.debug:
        err_args = conn.multipart_args
        if 'u' in err_args and 'p' in err_args:
            if not (
                p := await glob.players.get_login(
                    name = unquote(err_args['u']),
                    pw_md5 = err_args['p']
                )
            ):
                # player login incorrect
                await utils.misc.log_strange_occurrence('osu-error auth failed')
                p = None
        else:
            p = None

        err_desc = '{feedback} ({exception})'.format(**err_args)
        log(f'{p or "Offline user"} sent osu-error: {err_desc}', Ansi.LCYAN)
        printc(err_args['stacktrace'][:-2], Ansi.LMAGENTA)

    # TODO: save error in db
    pass

@domain.route('/web/osu-screenshot.php', methods=['POST'])
@required_mpargs({'u', 'p', 'v'})
@get_login(name_p='u', pass_p='p')
async def osuScreenshot(p: 'Player', conn: Connection) -> Optional[bytes]:
    if 'ss' not in conn.files:
        log('Screenshot req missing file.', Ansi.LRED)
        return (400, b'Missing file.')

    ss_file = conn.files['ss']

    # png sizes: 1080p: ~300-800kB | 4k: ~1-2mB
    if len(ss_file) > (4 * 1024 * 1024):
        return (400, b'Screenshot file too large.')

    if (
        'v' not in conn.multipart_args or
        conn.multipart_args['v'] != '1'
    ):
        await utils.misc.log_strange_occurrence(
            f'v=1 missing from osu-screenshot mp args; {conn.multipart_args}'
        )

    if (
        ss_file[:4] == b'\xff\xd8\xff\xe0' and
        ss_file[6:11] == b'JFIF\x00'
    ):
        extension = 'jpeg'
    elif (
        ss_file[:8] == b'\x89PNG\r\n\x1a\n' and
        ss_file[-8] == b'\x49END\xae\x42\x60\x82'
    ):
        extension = 'png'
    else:
        return (400, b'Invalid file type.')

    letters = string.ascii_letters + string.digits
    
    while True:
        filename = f'{secrets.token_urlsafe(6)}.{extension}'
        screenshot_file = SCREENSHOTS_PATH / filename
        if not screenshot_file.exists():
            break

    screenshot_file.write_bytes(ss_file)

    log(f'{p} uploaded {filename}.')
    return filename.encode()


@domain.route('/web/osu-getfriends.php')
@required_args({'u', 'h'})
@get_login(name_p='u', pass_p='h')
async def osuGetFriends(p: 'Player', conn: Connection) -> Optional[bytes]:
    return '\n'.join(map(str, p.friends)).encode()

_gulag_osuapi_status_map = {
    0: 0,
    2: 1,
    3: 2,
    4: 3,
    5: 4
}
def gulag_to_osuapi_status(s: int) -> int:
    return _gulag_osuapi_status_map[s]

@domain.route('/web/osu-getbeatmapinfo.php', methods=['POST'])
@required_args({'u', 'h'})
@get_login(name_p='u', pass_p='h')
@acquire_db_conn(aiomysql.DictCursor)
async def osuGetBeatmapInfo(
    p: 'Player',
    conn: Connection,
    db_cursor: aiomysql.DictCursor
) -> Optional[bytes]:
    print('watafak')
    print(conn.body)
    print('watafak')
    data = orjson.loads(conn.body)

    num_requests = len(data['Filenames']) + len(data['Ids'])
    log(f'{p} requested info for {num_requests} maps.', Ansi.LCYAN)

    ret = []

    for idx, map_filename in enumerate(data['Filenames']):
        # try getting the map from sql
        await db_cursor.execute(
            'SELECT id, set_id, status, md5 '
            'FROM maps '
            'WHERE filename = %s',
            [map_filename]
        )

        if db_cursor.rowcount == 0:
            continue # no map found

        res = await db_cursor.fetchone()

        # convert from gulag -> osu!api status
        res['status'] = gulag_to_osuapi_status(res['status'])

        # try to get the user's grades on the map osu!
        # only allows us to send back one per gamemode,
        # so we'll just send back relax for the time being..
        # XXX: perhaps user-customizable in the future?
        grades = ['N', 'N', 'N', 'N']

        await db_cursor.execute(
            'SELECT grade, mode FROM scores_rx '
            'WHERE map_md5 = %s AND userid = %s '
            'AND status = 2',
            [res['md5'], p.id]
        )

        async for score in db_cursor:
            grades[score['mode']] = score['grade']

        ret.append(
            '{i}|{id}|{set_id}|{md5}|{status}|{grades}'.format(
                **res,
                i=idx,
                grades='|'.join(grades)
            )
        )

    if data['Ids']: # still have yet to see this used
        await utils.misc.log_strange_occurrence(
            f'{p} requested map(s) info by id ({data["Ids"]})'
        )

    return '\n'.join(ret).encode()


@domain.route('/web/osu-getfavourites.php')
@required_args({'u', 'h'})
@get_login(name_p='u', pass_p='h')
async def osuGetFavourites(p: 'Player', conn: Connection) -> Optional[bytes]:
    favourites = await glob.db.fetchall(
        'SELECT setid FROM favourites '
        'WHERE userid = %s',
        [p.id]
    )

    return '\n'.join(favourites).encode()


@domain.route('/web/osu-addfavourite.php')
@required_args({'u', 'h', 'a'})
@get_login(name_p='u', pass_p='h', auth_error=b'Please login to add favourites!')
async def osuAddFavourite(p: 'Player', conn: Connection) -> Optional[bytes]:
    # make sure set id is valid
    if not conn.args['a'].isdecimal():
        return (400, b'Invalid beatmap set id.')

    # check if they already have this favourited.
    if await glob.db.fetch(
        'SELECT 1 FROM favourites '
        'WHERE userid = %s AND setid = %s',
        [p.id, conn.args['a']]
    ):
        return b"You've already favourited this beatmap!"

    # add favourite
    await glob.db.execute(
        'INSERT INTO favourites '
        'VALUES (%s, %s)',
        [p.id, conn.args['a']]
    )


@domain.route('/web/lastfm.php')
@required_args({'b', 'action', 'us', 'ha'})
@get_login(name_p='us', pass_p='ha')
async def lastFM(p: 'Player', conn: Connection) -> Optional[bytes]:
    if conn.args['b'][0] != 'a':
        # not anticheat related, tell the
        # client not to send any more for now.
        return b'-3'

    flags = ClientFlags(int(conn.args['b'][1:]))

    if flags & (ClientFlags.HQAssembly | ClientFlags.HQFile):
        # Player is currently running hq!osu; could possibly
        # be a separate client, buuuut prooobably not lol.

        await p.restrict(
            admin=glob.bot,
            reason=f'hq!osu running ({flags})'
        )
        return b'-3'

    if flags & ClientFlags.RegistryEdits:
        # Player has registry edits left from
        # hq!osu's multiaccounting tool. This
        # does not necessarily mean they are
        # using it now, but they have in the past.

        if random.randrange(32) == 0:
            # Random chance (1/32) for a ban.
            await p.restrict(
                admin = glob.bot,
                reason = 'hq!osu relife 1/32'
            )
            return b'-3'

        # TODO: make a tool to remove the flags & send this as a dm.
        #       also add to db so they never are restricted on first one.
        p.enqueue(packets.notification('\n'.join([
            "Hey!",
            "It appears you have hq!osu's multiaccounting tool (relife) enabled.",
            "This tool leaves a change in your registry that the osu! client can detect.",
            "Please re-install relife and disable the program to avoid any restrictions."
        ])))

        p.logout()

        return b'-3'

    """ These checks only worked for ~5 hours from release. rumoi's quick!
    if flags & (ClientFlags.libeay32Library | ClientFlags.aqnMenuSample):
        # AQN has been detected in the client, either
        # through the 'libeay32.dll' library being found
        # onboard, or from the menu sound being played in
        # the AQN menu while being in an inappropriate menu
        # for the context of the sound effect.
        pass
    """

# gulag supports both cheesegull mirrors & chimu.moe.
# chimu.moe handles things a bit differently than cheesegull,
# and has some extra features we'll eventually use more of.
USING_CHIMU = 'chimu.moe' in glob.config.mirror

DIRECT_SET_INFO_FMTSTR = (
    '{{{setid_spelling}}}.osz|{{Artist}}|{{Title}}|{{Creator}}|'
    '{{RankedStatus}}|10.0|{{LastUpdate}}|{{{setid_spelling}}}|'
    '0|{{HasVideo}}|0|0|0|{{diffs}}' # 0s are threadid, has_story,
                                     # filesize, filesize_novid.
).format(setid_spelling='SetId' if USING_CHIMU else 'SetID')

DIRECT_MAP_INFO_FMTSTR = (
    '[{DifficultyRating:.2f}⭐] {DiffName} '
    '{{cs: {CS} / od: {OD} / ar: {AR} / hp: {HP}}}@{Mode}'
)

@domain.route('/web/osu-search.php')
@required_args({'u', 'h', 'r', 'q', 'm', 'p'})
@get_login(name_p='u', pass_p='h')
async def osuSearchHandler(p: 'Player', conn: Connection) -> Optional[bytes]:
    if not conn.args['p'].isdecimal():
        return (400, b'')

    if not glob.has_internet:
        return b'-1\nosu!direct requires an internet connection.'

    if USING_CHIMU:
        search_url = f'{glob.config.mirror}/search'
    else:
        search_url = f'{glob.config.mirror}/api/search'

    params = {
        'amount': 100,
        'offset': int(conn.args['p']) * 100
    }

    # eventually we could try supporting these,
    # but it mostly depends on the mirror.
    if conn.args['q'] not in ('Newest', 'Top+Rated', 'Most+Played'):
        params['query'] = conn.args['q'].replace('+', ' ')

    if conn.args['m'] != '-1':
        params['mode'] = conn.args['m']

    if conn.args['r'] != '4':  # 4 = all
        # convert to osu!api status
        status = RankedStatus.from_osudirect(int(conn.args['r']))
        params['status'] = status.osu_api

    async with glob.http.get(search_url, params=params) as resp:
        if not resp:
            stacktrace = utils.misc.get_appropriate_stacktrace()
            await utils.misc.log_strange_occurrence(stacktrace)

        if USING_CHIMU: # error handling varies
            if resp.status == 404:
                return b'0' # no maps found
            elif resp.status == 502: # bad gateway, happens a lot with chimu :/
                return b'-1\nFailed to retrieve data from the beatmap mirror.'
            elif resp.status != 200:
                stacktrace = utils.misc.get_appropriate_stacktrace()
                await utils.misc.log_strange_occurrence(stacktrace)
                return b'-1\nFailed to retrieve data from the beatmap mirror.'
        else: # cheesegull
            if resp.status != 200:
                return b'-1\nFailed to retrieve data from the beatmap mirror.'

        result = await resp.json()

        if USING_CHIMU:
            if result['code'] != 0:
                stacktrace = utils.misc.get_appropriate_stacktrace()
                await utils.misc.log_strange_occurrence(stacktrace)
                return b'-1\nFailed to retrieve data from the beatmap mirror.'
            result = result['data']

    lresult = len(result) # send over 100 if we receive
                          # 100 matches, so the client
                          # knows there are more to get
    ret = [f"{'101' if lresult == 100 else lresult}"]

    for bmap in result:
        if bmap['ChildrenBeatmaps'] is None:
            continue

        if USING_CHIMU:
            bmap['HasVideo'] = int(bmap['HasVideo'])
        else:
            # cheesegull doesn't support vids
            bmap['HasVideo'] = '0'

        diff_sorted_maps = sorted(
            bmap['ChildrenBeatmaps'],
            key = lambda m: m['DifficultyRating']
        )
        diffs_str = ','.join([DIRECT_MAP_INFO_FMTSTR.format(**row)
                              for row in diff_sorted_maps])

        ret.append(DIRECT_SET_INFO_FMTSTR.format(**bmap, diffs=diffs_str))

    return '\n'.join(ret).encode()

# TODO: video support (needs db change)
@domain.route('/web/osu-search-set.php')
@required_args({'u', 'h'})
@get_login(name_p='u', pass_p='h')
async def osuSearchSetHandler(p: 'Player', conn: Connection) -> Optional[bytes]:
    # Since we only need set-specific data, we can basically
    # just do same same query with either bid or bsid.
    if 's' in conn.args:
        # gulag chat menu: if the argument is negative,
        # check if it's in the players menu options.
        if conn.args['s'][0] == '-':
            opt_id = int(conn.args['s'])

            if opt_id not in p.menu_options:
                return b'no voila'

            opt = p.menu_options[opt_id]

            if time.time() > opt['timeout']:
                # the option has expired.
                del p.menu_options[opt_id]
                return

            # we have a menu option. activate it.
            await opt['callback']()

            if not opt['reusable']:
                # remove the option from the player
                del p.menu_options[opt_id]

            # send back some random syntactically valid
            # beatmap info so that the client doesn't open
            # a webpage when clicking an unknown url.
            return b'voila'
        else:
            # this is just a normal request
            k, v = ('set_id', conn.args['s'])
    elif 'b' in conn.args:
        k, v = ('id', conn.args['b'])
    else:
        return  # invalid args

    # Get all set data.
    bmapset = await glob.db.fetch(
        'SELECT DISTINCT set_id, artist, '
        'title, status, creator, last_update '
        f'FROM maps WHERE {k} = %s', [v]
    )

    if not bmapset:
        # TODO: get from osu!
        return

    return ('{set_id}.osz|{artist}|{title}|{creator}|'
            '{status}|10.0|{last_update}|{set_id}|'  # TODO: rating
            '0|0|0|0|0').format(**bmapset).encode()
    # 0s are threadid, has_vid, has_story, filesize, filesize_novid

def chart_entry(name: str, before: Optional[object], after: object) -> str:
    return f'{name}Before:{before or ""}|{name}After:{after}'

@domain.route('/web/osu-submit-modular-selector.php', methods=['POST'])
@required_mpargs({'x', 'ft', 'score', 'fs', 'bmk', 'iv',
                  'c1', 'st', 'pass', 'osuver', 's'})
@acquire_db_conn(aiomysql.DictCursor)
async def osuSubmitModularSelector(
    conn: Connection,
    db_cursor: aiomysql.DictCursor
) -> Optional[bytes]:
    mp_args = conn.multipart_args

    # Parse our score data into a score obj.
    score = await Score.from_submission(
        data_b64=mp_args['score'], iv_b64=mp_args['iv'],
        osu_ver=mp_args['osuver'], pw_md5=mp_args['pass']
    )

    if not score:
        log('Failed to parse a score - invalid format.', Ansi.LRED)
        return b'error: no'
    elif not score.player:
        # Player is not online, return nothing so that their
        # client will retry submission when they log in.
        return
    elif not score.bmap:
        # Map does not exist, most likely unsubmitted.
        return b'error: beatmap'
    elif score.bmap.status == RankedStatus.Pending:
        # XXX: Perhaps will accept in the future,
        return b'error: no'  # not now though.

    # we should update their activity no matter
    # what the result of the score submission is.
    score.player.update_latest_activity()

    # attempt to update their stats if their
    # gm/gm-affecting-mods change at all.
    if score.mode != score.player.status.mode:
        score.player.status.mods = score.mods
        score.player.status.mode = score.mode

        if not score.player.restricted:
            glob.players.enqueue(packets.userStats(score.player))

    scores_table = score.mode.scores_table
    mode_vn = score.mode.as_vanilla

    # Check for score duplicates
    # TODO: this it quite the bandaid fix, not that other
    # implementations do it better.. still though, perhaps
    # it would be worth going through a hardcoded number or
    # percent of the replay's frames to really determine
    # whether the plays are the same, rather than just
    # using the score/header data.
    await db_cursor.execute(
        f'SELECT 1 FROM {scores_table} '
        'WHERE play_time > DATE_SUB(NOW(), INTERVAL 2 MINUTE) ' # last 2mins
        'AND mode = %s AND map_md5 = %s '
        'AND userid = %s AND mods = %s '
        'AND score = %s AND play_time', [
            mode_vn, score.bmap.md5,
            score.player.id, score.mods, score.score
        ]
    )
    res = await db_cursor.fetchone()

    if res:
        log(f'{score.player} submitted a duplicate score.', Ansi.LYELLOW)
        return b'error: no'

    time_elapsed = mp_args['st' if score.passed else 'ft']

    if not time_elapsed.isdecimal():
        return (400, b'?')

    score.time_elapsed = int(time_elapsed)

    if 'i' in conn.files:
        stacktrace = utils.misc.get_appropriate_stacktrace()
        await utils.misc.log_strange_occurrence(stacktrace)

    if ( # check for pp caps on ranked & approved maps for appropriate players.
        score.bmap.status != RankedStatus.Loved and not (
            score.player.priv & Privileges.Whitelisted or
            score.player.restricted
        )
    ):
        # Get the PP cap for the current context.
        pp_cap = glob.config.autoban_pp[score.mode][score.mods & Mods.FLASHLIGHT != 0]

        if score.pp > pp_cap:
            await score.player.restrict(
                admin=glob.bot,
                reason=f'[{score.mode!r} {score.mods!r}] autoban @ {score.pp:.2f}pp'
            )

    if score.mods & Mods.SCOREV2:
        return b'error: no'

    """ Score submission checks completed; submit the score. """

    if glob.datadog:
        glob.datadog.increment('gulag.submitted_scores')

    if score.status == SubmissionStatus.BEST:
        if glob.datadog:
            glob.datadog.increment('gulag.submitted_scores_best')

        if score.rank == 1 and not score.player.restricted:
            # this is the new #1, post the play to #announce.
            announce_chan = glob.channels['#announce']

            if score.bmap.awards_pp:
                performance = f'{score.pp:,.2f}pp'
            else:
                performance = f'{score.score:,} score'

            # Announce the user's #1 score.
            # TODO: truncate artist/title/version to fit on screen
            ann = [f'\x01ACTION achieved #1 on {score.bmap.embed}',
                   f'with {score.acc:.2f}% for {performance}.']

            if score.mods:
                ann.insert(1, f'+{score.mods!r}')

            scoring_metric = 'pp' if score.mode >= GameMode.rx_std else 'score'

            # If there was previously a score on the map, add old #1.
            await db_cursor.execute(
                'SELECT u.id, name FROM users u '
                f'INNER JOIN {scores_table} s ON u.id = s.userid '
                'WHERE s.map_md5 = %s AND s.mode = %s '
                'AND s.status = 2 AND u.priv & 1 '
                f'ORDER BY s.{scoring_metric} DESC LIMIT 1',
                [score.bmap.md5, mode_vn]
            )

            if db_cursor.rowcount != 0:
                prev_n1 = await db_cursor.fetchone()

                if score.player.id != prev_n1['id']:
                    pid = prev_n1['id']
                    pname = prev_n1['name']
                    ann.append(f'(Previous #1: [https://{BASE_DOMAIN}/u/{pid} {pname}])')

            score.player.enqueue(packets.notification(f'You achieved #1! ({performance})'))
            announce_chan.send(' '.join(ann), sender=score.player, to_self=True)

        # this score is our best score.
        # update any preexisting personal best
        # records with SubmissionStatus.SUBMITTED.
        await db_cursor.execute(
            f'UPDATE {scores_table} SET status = 1 '
            'WHERE status = 2 AND map_md5 = %s '
            'AND userid = %s AND mode = %s',
            [score.bmap.md5, score.player.id, mode_vn]
        )

    await db_cursor.execute(
        f'INSERT INTO {scores_table} '
        'VALUES (NULL, '
        '%s, %s, %s, %s, '
        '%s, %s, %s, %s, '
        '%s, %s, %s, %s, '
        '%s, %s, %s, %s, '
        '%s, %s, %s, %s, %s)', [
            score.bmap.md5, score.score, score.pp, score.acc, score.max_combo, score.mods,
            score.n300, score.n100, score.n50, score.nmiss, score.ngeki, score.nkatu,
            score.grade, score.status, mode_vn, score.play_time,
            score.time_elapsed, score.client_flags, score.player.id, score.perfect, 0
        ]
    )
    score.id = db_cursor.lastrowid

    if score.passed:
        # All submitted plays should have a replay.
        # If not, they may be using a score submitter.
        replay_missing = (
            'score' not in conn.files or
            conn.files['score'] == b'\r\n'
        )

        if replay_missing and not score.player.restricted:
            log(f'{score.player} submitted a score without a replay!', Ansi.LRED)
            await score.player.restrict(
                admin = glob.bot,
                reason = 'submitted score with no replay'
            )
        else:
            # TODO: the replay is currently sent from the osu!
            # client compressed with LZMA; this compression can
            # be improved pretty decently by serializing it
            # manually, so we'll probably do that in the future.
            replay_file = REPLAYS_PATH / f'{score.id}.osr'
            replay_file.write_bytes(conn.files['score'])

            # TODO: if a play is sketchy.. 🤠
            # await glob.sketchy_queue.put(s)

    """ Update the user's & beatmap's stats """

    # get the current stats, and take a
    # shallow copy for the response charts.
    stats = score.player.gm_stats
    prev_stats = copy.copy(stats)

    # update plays & playtime for all submitted scores
    stats.playtime += score.time_elapsed // 1000
    stats.plays += 1

    mode_sql = format(score.mode, 'sql')
    stats_query = [ # build a list of params to update
        'UPDATE stats SET plays_{mode} = %s',
        'playtime_{mode} = %s'
    ]
    stats_params = [stats.plays, stats.playtime]

    if score.passed and score.bmap.awards_pp:
        # submitted score on a ranked map,
        # update max combo and total score.

        # update max combo
        if score.max_combo > stats.max_combo:
            stats.max_combo = score.max_combo
            stats_query.append('max_combo_{mode} = %s')
            stats_params.append(stats.max_combo)

        # update total score
        stats.tscore += score.score
        stats_query.append('tscore_{mode} = %s')
        stats_params.append(stats.tscore)

        if score.status == SubmissionStatus.BEST:
            # our (new) best score on the map,
            # update ranked score, pp, acc, and rank.

            # update ranked score
            additional_rscore = score.score
            if score.prev_best:
                # we previously had a score, so remove
                # it's score from our ranked score.
                additional_rscore -= score.prev_best.score
            stats.rscore += additional_rscore
            stats_query.append('rscore_{mode} = %s')
            stats_params.append(stats.rscore)

            # fetch scores sorted by pp for total acc/pp calc
            # NOTE: we select all plays (and not just top100)
            # because bonus pp counts the total amount of ranked
            # scores. i'm aware this scales horribly and it'll
            # likely be split into two queries in the future.
            await db_cursor.execute(
                f'SELECT s.pp, s.acc FROM {scores_table} s '
                'INNER JOIN maps m ON s.map_md5 = m.md5 '
                'WHERE s.userid = %s AND s.mode = %s '
                'AND s.status = 2 AND m.status IN (2, 3) ' # ranked, approved
                'ORDER BY s.pp DESC',
                [score.player.id, mode_vn]
            )

            total_scores = db_cursor.rowcount
            top_100_pp = await db_cursor.fetchmany(size=100)

            # update total weighted accuracy
            tot = div = 0
            for i, row in enumerate(top_100_pp):
                add = int((0.95 ** i) * 100)
                tot += row['acc'] * add
                div += add
            stats.acc = tot / div
            stats_query.append('acc_{mode} = %s')
            stats_params.append(stats.acc)

            # update total weighted pp
            weighted_pp = sum([row['pp'] * 0.95 ** i
                               for i, row in enumerate(top_100_pp)])
            bonus_pp = 416.6667 * (1 - 0.9994 ** total_scores)
            stats.pp = round(weighted_pp + bonus_pp)
            stats_query.append('pp_{mode} = %s')
            stats_params.append(stats.pp)

            # update rank
            # TODO: adjust any people inbetween we passed,
            # check whether they're online, and push their
            # stats to all online players if nescessary.
            await db_cursor.execute(
                'SELECT COUNT(*) AS higher_pp_players '
                'FROM stats s '
                'INNER JOIN users u USING(id) '
                f'WHERE s.pp_{mode_sql} > %s '
                'AND u.priv & 1 and u.id != %s',
                [stats.pp, score.player.id]
            )
            stats.rank = 1 + (await db_cursor.fetchone())['higher_pp_players']

    # construct the sql query of any stat changes
    stats_query = f"{','.join(stats_query).format(mode=mode_sql)} WHERE id = %s"
    stats_params.append(score.player.id)

    # send any stat changes to sql, and other players
    await db_cursor.execute(stats_query, stats_params)
    glob.players.enqueue(packets.userStats(score.player))

    if not score.player.restricted:
        # update beatmap with new stats
        score.bmap.plays += 1
        if score.passed:
            score.bmap.passes += 1

        await db_cursor.execute(
            'UPDATE maps SET plays = %s, '
            'passes = %s WHERE md5 = %s',
            [score.bmap.plays, score.bmap.passes, score.bmap.md5]
        )

    # update their recent score
    score.player.recent_scores[score.mode] = score
    if 'recent_score' in score.player.__dict__:
        del score.player.recent_score  # wipe cached_property

    """ score submission charts """

    if not score.passed or score.mode >= GameMode.rx_std:
        # basically, the osu! client and the way bancho handles this
        # is dumb. if you submit a failed play on bancho, it will
        # still generate the charts and send it to the client, even
        # when the client can't (and doesn't use them).. so instead,
        # we'll send back an empty error, which will just tell the
        # client that the score submission process is complete.. lol
        # (also no point on rx/ap since you can't see the charts atm xd)

        # TODO: we actually have to send back an empty chart since the
        # client uses this to confirm the score has been submitted.. lol
        ret = b'error: no'

    else:
        # prepare to send the user charts & achievements.
        achievements = []

        # achievements unlocked only for non-restricted players
        if not score.player.restricted:
            if score.bmap.awards_pp:
                for ach in glob.achievements:
                    if ach in score.player.achievements:
                        # player already has this achievement.
                        continue

                    if ach.cond(score, mode_vn):
                        await score.player.unlock_achievement(ach)
                        achievements.append(ach)

        # XXX: really not a fan of how this is done atm,
        # but it's kinda just something that's probably
        # going to be ugly no matter what i do lol :v
        charts = []

        # append beatmap info chart (#1)
        charts.append(
            f'beatmapId:{score.bmap.id}|'
            f'beatmapSetId:{score.bmap.set_id}|'
            f'beatmapPlaycount:{score.bmap.plays}|'
            f'beatmapPasscount:{score.bmap.passes}|'
            f'approvedDate:{score.bmap.last_update}'
        )

        # append beatmap ranking chart (#2)
        charts.append('|'.join((
            'chartId:beatmap',
            f'chartUrl:{score.bmap.set.url}',
            'chartName:Beatmap Ranking',

            *((
                chart_entry('rank', score.prev_best.rank, score.rank),
                chart_entry('rankedScore', score.prev_best.score, score.score),
                chart_entry('totalScore', score.prev_best.score, score.score),
                chart_entry('maxCombo', score.prev_best.max_combo, score.max_combo),
                chart_entry('accuracy', round(score.prev_best.acc, 2), round(score.acc, 2)),
                chart_entry('pp', score.prev_best.pp, score.pp)
            ) if score.prev_best else (
                chart_entry('rank', None, score.rank),
                chart_entry('rankedScore', None, score.score),
                chart_entry('totalScore', None, score.score),
                chart_entry('maxCombo', None, score.max_combo),
                chart_entry('accuracy', None, round(score.acc, 2)),
                chart_entry('pp', None, score.pp)
            )),

            f'onlineScoreId:{score.id}'
        )))

        # append overall ranking chart (#3)
        charts.append('|'.join((
            'chartId:overall',
            f'chartUrl:https://{BASE_DOMAIN}/u/{score.player.id}',
            'chartName:Overall Ranking',

            *((
                chart_entry('rank', prev_stats.rank, stats.rank),
                chart_entry('rankedScore', prev_stats.rscore, stats.rscore),
                chart_entry('totalScore', prev_stats.tscore, stats.tscore),
                chart_entry('maxCombo', prev_stats.max_combo, stats.max_combo),
                chart_entry('accuracy', round(prev_stats.acc, 2), round(stats.acc, 2)),
                chart_entry('pp', prev_stats.pp, stats.pp),
            ) if prev_stats else (
                chart_entry('rank', None, stats.rank),
                chart_entry('rankedScore', None, stats.rscore),
                chart_entry('totalScore', None, stats.tscore),
                chart_entry('maxCombo', None, stats.max_combo),
                chart_entry('accuracy', None, round(stats.acc, 2)),
                chart_entry('pp', None, stats.pp),
            )),

            f'achievements-new:{"/".join(map(repr, achievements))}'
        )))

        ret = '\n'.join(charts).encode()
    
    notif_caps = glob.config.notif_caps[score.mode]

    if score.status == SubmissionStatus.BEST and score.pp >= notif_caps and not score.player.restricted:
        webhook_url = glob.config.webhooks['scores']
        webhook = Webhook(url=webhook_url)

        if not score.max_combo <= score.max_combo - 20 and score.nmiss == 0:
            status = "FC"
        else:
            if score.nmiss > 0:
                status = f'{score.max_combo}/{score.bmap.max_combo} {score.nmiss}xMiss'
            else:
                status = f'{score.max_combo}/{score.bmap.max_combo} SB'
        
        embed = Embed(
            title = f'__New {score.status!r} Score! **{score.pp:.2f}pp**__',
            description = f'▸ [{score.mode!r}] • #{stats.rank} • {stats.pp}pp • {stats.acc:.2f}%\n▸ {status} • {score.grade} • {score.mods!r} • {score.acc:.2f}%\n[{score.bmap.artist} - {score.bmap.title} [{score.bmap.version}]](https://sakuru.pw/direct?id={score.bmap.set_id})',
            color=0xbb0ebe,
            timestamp = datetime.datetime.utcnow()
        )

        embed.set_author(
            url = score.player.url,
            name = score.player.name,
            icon_url = score.player.avatar_url
        )

        embed.set_image(url=f'https://assets.ppy.sh/beatmaps/{score.bmap.set_id}/covers/cover.jpg')
        embed.set_footer(
            text=f"played on sakuru.pw"
        )
        webhook.add_embed(embed)
        await webhook.post(glob.http)

    log(f'[{score.mode!r}] {score.player} submitted a score! '
        f'({score.status!r}, {score.pp:,.2f}pp / {stats.pp:,}pp)', Ansi.LGREEN)

    return ret


SCOREID_BORDERS = tuple(
    (((1 << 63) - 1) // 3) * i
    for i in range(1, 4)
)


@domain.route('/web/osu-getreplay.php')
@required_args({'u', 'h', 'm', 'c'})
@get_login(name_p='u', pass_p='h')
async def getReplay(p: 'Player', conn: Connection) -> Optional[bytes]:
    if 'c' not in conn.args or not conn.args['c'].isdecimal():
        return  # invalid connection

    i64_max = (1 << 63) - 1

    if not 0 < (score_id := int(conn.args['c'])) <= i64_max:
        return # invalid score id

    replay_file = REPLAYS_PATH / f'{score_id}.osr'

    # osu! expects empty resp for no replay
    if replay_file.exists():
        file_bytes = replay_file.read_bytes()

        if SCOREID_BORDERS[0] > score_id >= 1:
            scores_table = 'scores_vn'
        elif SCOREID_BORDERS[1] > score_id >= SCOREID_BORDERS[0]:
            scores_table = 'scores_rx'
        elif SCOREID_BORDERS[2] > score_id >= SCOREID_BORDERS[1]:
            scores_table = 'scores_ap'
        else:
            return

        userid = await glob.db.fetch(f'SELECT userid FROM {scores_table} WHERE id = {score_id}')

        if p.id != userid.get('userid'):
            await glob.db.execute(f'UPDATE {scores_table} SET watched = watched + 1 WHERE id = {score_id}')

        if len(str(struct.unpack('<i', file_bytes[1:5])[0])) == 8:
            parsed = parse_replay_file(replay_file)
            return parsed.lzma_string
        else:
            return file_bytes


@domain.route('/web/osu-rate.php')
@required_args({'u', 'p', 'c'})
@get_login(name_p='u', pass_p='p', auth_error=b'auth fail')
@acquire_db_conn(aiomysql.Cursor)
async def osuRate(
    p: 'Player',
    conn: Connection,
    db_cursor: aiomysql.Cursor
) -> Optional[bytes]:
    map_md5 = conn.args['c']

    if 'v' not in conn.args:
        # check if we have the map in our cache;
        # if not, the map probably doesn't exist.
        if map_md5 not in glob.cache['beatmap']:
            return b'no exist'

        cached = glob.cache['beatmap'][map_md5]

        # only allow rating on maps with a leaderboard.
        if cached.status < RankedStatus.Ranked:
            return b'not ranked'

        # osu! client is checking whether we can rate the map or not.
        await db_cursor.execute(
            'SELECT 1 FROM ratings WHERE '
            'map_md5 = %s AND userid = %s',
            [map_md5, p.id]
        )

        # the client hasn't rated the map, so simply
        # tell them that they can submit a rating.
        if db_cursor.rowcount == 0:
            return b'ok'
    else:
        # the client is submitting a rating for the map.
        if not (rating := conn.args['v']).isdecimal():
            return

        await db_cursor.execute(
            'INSERT INTO ratings '
            'VALUES (%s, %s, %s)',
            [p.id, map_md5, int(rating)]
        )

    await db_cursor.execute(
        'SELECT rating FROM ratings '
        'WHERE map_md5 = %s',
        [map_md5]
    )
    ratings = [row[0] async for row in await db_cursor]

    # send back the average rating
    avg = sum(ratings) / len(ratings)
    return f'alreadyvoted\n{avg}'.encode()


@unique
@pymysql_encode(escape_enum)
class RankingType(IntEnum):
    Local = 0
    Top = 1
    Mods = 2
    Friends = 3
    Country = 4

SCORE_LISTING_FMTSTR = (
    '{id}|{name}|{score}|{max_combo}|'
    '{n50}|{n100}|{n300}|{nmiss}|{nkatu}|{ngeki}|'
    '{perfect}|{mods}|{userid}|{rank}|{time}|{has_replay}'
)

@domain.route('/web/osu-osz2-getscores.php')
@required_args({'s', 'vv', 'v', 'c', 'f', 'm',
                'i', 'mods', 'h', 'a', 'us', 'ha'})
@get_login(name_p='us', pass_p='ha')
@acquire_db_conn(aiomysql.DictCursor)
async def getScores(
    p: 'Player',
    conn: Connection,
    db_cursor: aiomysql.DictCursor
) -> Optional[bytes]:
    if not all([ # make sure all int args are integral
        _isdecimal(conn.args[k], _negative=True)
        for k in ('mods', 'v', 'm', 'i')
    ]):
        return b'-1|false'

    map_md5 = conn.args['c']

    # check if this md5 has already been  cached as
    # unsubmitted/needs update to reduce osu!api spam
    if map_md5 in glob.cache['unsubmitted']:
        return b'-1|false'
    if map_md5 in glob.cache['needs_update']:
        return b'1|false'

    mods = Mods(int(conn.args['mods']))
    mode_vn = int(conn.args['m'])

    mode = GameMode.from_params(mode_vn, mods)

    map_set_id = int(conn.args['i'])
    has_set_id = map_set_id > 0

    rank_type = RankingType(int(conn.args['v']))

    # attempt to update their stats if their
    # gm/gm-affecting-mods change at all.
    if mode != p.status.mode:
        p.status.mods = mods
        p.status.mode = mode

        if not p.restricted:
            glob.players.enqueue(packets.userStats(p))

    if(p.priv & Privileges.Donator and mods & Mods.AUTOPLAY):
        scoring_metric = 'pp'
    else:
        scoring_metric = 'pp' if mode >= GameMode.rx_std else 'score'

    scores_table = mode.scores_table

    bmap = await Beatmap.from_md5(map_md5, set_id=map_set_id)

    if not bmap:
        # map not found, figure out whether it needs an
        # update or isn't submitted using it's filename.

        if (
            has_set_id and
            map_set_id not in glob.cache['beatmapset']
        ):
            # set not cached, it doesn't exist
            glob.cache['unsubmitted'].add(map_md5)
            return b'-1|false'

        map_filename = unquote(conn.args['f'].replace('+', ' '))

        if has_set_id:
            # we can look it up in the specific set from cache
            for bmap in glob.cache['beatmapset'][map_set_id].maps:
                if map_filename == bmap.filename:
                    map_exists = True
                    break
            else:
                map_exists = False
        else:
            # we can't find it on the osu!api by md5,
            # and we don't have the set id, so we must
            # look it up in sql from the filename.
            map_exists = await glob.db.fetch(
                'SELECT 1 FROM maps '
                'WHERE filename = %s',
                [map_filename]
            ) is not None

        if map_exists:
            # map can be updated.
            glob.cache['needs_update'].add(map_md5)
            return b'1|false'
        else:
            # map is unsubmitted.
            # add this map to the unsubmitted cache, so
            # that we don't have to make this request again.
            print('pizdec')
            glob.cache['unsubmitted'].add(map_md5)
            return b'-1|false'

    # we've found a beatmap for the request.

    if glob.datadog:
        glob.datadog.increment('gulag.leaderboards_served')

    if bmap.status < RankedStatus.Ranked:
        # only show leaderboards for ranked,
        # approved, qualified, or loved maps.
        return f'{int(bmap.status)}|false'.encode()

    # statuses: 0: failed, 1: passed but not top, 2: passed top
    query = [
        f"SELECT s.id, s.{scoring_metric} AS _score, "
        "s.max_combo, s.n50, s.n100, s.n300, "
        "s.nmiss, s.nkatu, s.ngeki, s.perfect, s.mods, "
        "UNIX_TIMESTAMP(s.play_time) time, u.id userid, "
        "COALESCE(CONCAT('[', c.tag, '] ', u.name), u.name) AS name "
        f"FROM {scores_table} s "
        "INNER JOIN users u ON u.id = s.userid "
        "LEFT JOIN clans c ON c.id = u.clan_id "
        "WHERE s.map_md5 = %s AND s.status = 2 "
        "AND (u.priv & 1 OR u.id = %s) AND mode = %s"
    ]

    params = [map_md5, p.id, mode_vn]

    if rank_type == RankingType.Mods:
        query.append('AND s.mods = %s')
        params.append(mods)
    elif rank_type == RankingType.Friends:
        if p.priv & Privileges.Donator:
            friends_str = ','.join(map(str, p.friends))
            query.append(f'AND s.userid IN ({friends_str}, {p.id})')
    elif rank_type == RankingType.Country:
        query.append('AND u.country = %s')
        params.append(p.country[1])  # letters, not id

    query.append('ORDER BY _score DESC LIMIT 50')

    await db_cursor.execute(' '.join(query), params)
    num_scores = db_cursor.rowcount
    scores = await db_cursor.fetchall()

    l: list[str] = []

    # ranked status, serv has osz2, bid, bsid, len(scores)
    l.append(f'{int(bmap.status)}|false|{bmap.id}|{bmap.set_id}|{num_scores}')

    # fetch beatmap rating from sql
    await db_cursor.execute(
        'SELECT AVG(rating) rating '
        'FROM ratings '
        'WHERE map_md5 = %s',
        [bmap.md5]
    )
    rating = (await db_cursor.fetchone())['rating']

    if rating is not None:
        rating = f'{rating:.1f}'
    else:
        rating = '10.0'

    # TODO: we could have server-specific offsets for
    # maps that mods could set for incorrectly timed maps.
    l.append(f'0\n{bmap.full}\n{rating}') # offset, name, rating

    if not scores:
        # simply return an empty set.
        return ('\n'.join(l) + '\n\n').encode()

    # fetch player's personal best score
    await db_cursor.execute(
        f'SELECT id, {scoring_metric} AS _score, '
        'max_combo, n50, n100, n300, '
        'nmiss, nkatu, ngeki, perfect, mods, '
        'UNIX_TIMESTAMP(play_time) time '
        f'FROM {scores_table} '
        'WHERE map_md5 = %s AND mode = %s '
        'AND userid = %s AND status = 2 '
        'ORDER BY _score DESC LIMIT 1', [
            map_md5, mode_vn, p.id
        ]
    )
    p_best = await db_cursor.fetchone()

    if p_best:
        # calculate the rank of the score.
        await db_cursor.execute(
            f'SELECT COUNT(*) AS count FROM {scores_table} s '
            'INNER JOIN users u ON u.id = s.userid '
            'WHERE s.map_md5 = %s AND s.mode = %s '
            'AND s.status = 2 AND u.priv & 1 '
            f'AND s.{scoring_metric} > %s', [
                map_md5, mode_vn,
                p_best['_score']
            ]
        )
        p_best_rank = 1 + (await db_cursor.fetchone())['count']

        l.append(
            SCORE_LISTING_FMTSTR.format(
                **p_best,
                name=p.full_name,
                userid=p.id,
                score=int(p_best['_score']),
                has_replay='1',
                rank=p_best_rank
            )
        )
    else:
        l.append('')

    l.extend([
        SCORE_LISTING_FMTSTR.format(
            **s,
            score=int(s['_score']),
            has_replay='1',
            rank=idx + 1
        ) for idx, s in enumerate(scores)
    ])

    return '\n'.join(l).encode()


@domain.route('/web/osu-comment.php', methods=['POST'])
@required_mpargs({'u', 'p', 'b', 's',
                  'm', 'r', 'a'})
@get_login(name_p='u', pass_p='p')
async def osuComment(p: 'Player', conn: Connection) -> Optional[bytes]:
    mp_args = conn.multipart_args

    action = mp_args['a']

    if action == 'get':
        # client is requesting all comments
        comments = await glob.db.fetchall(
            "SELECT c.time, c.target_type, c.colour, "
            "c.comment, u.priv FROM comments c "
            "INNER JOIN users u ON u.id = c.userid "
            "WHERE (c.target_type = 'replay' AND c.target_id = %s) "
            "OR (c.target_type = 'song' AND c.target_id = %s) "
            "OR (c.target_type = 'map' AND c.target_id = %s) ",
            [mp_args['r'], mp_args['s'], mp_args['b']]
        )

        ret: list[str] = []

        for cmt in comments:
            # TODO: maybe support player/creator colours?
            # pretty expensive for very low gain, but completion :D
            if cmt['priv'] & Privileges.Nominator:
                fmt = 'bat'
            elif cmt['priv'] & Privileges.Donator:
                fmt = 'supporter'
            else:
                fmt = ''

            if cmt['colour']:
                fmt += f'|{cmt["colour"]}'

            ret.append('{time}\t{target_type}\t'
                       '{fmt}\t{comment}'.format(fmt=fmt, **cmt))

        p.update_latest_activity()
        return '\n'.join(ret).encode()

    elif action == 'post':
        # client is submitting a new comment

        # get the comment's target scope
        target_type = mp_args['target']
        if target_type not in ('song', 'map', 'replay'):
            return (400, b'Invalid target_type.')

        # get the corresponding id from the request
        target_id = mp_args[{'song': 's', 'map': 'b',
                             'replay': 'r'}[target_type]]

        if not target_id.isdecimal():
            return (400, b'Invalid target id.')

        # get some extra params
        sttime = mp_args['starttime']
        comment = mp_args['comment']

        if (
            'f' in mp_args and
            p.priv & Privileges.Donator
        ):
            # only supporters can use colours.
            # XXX: colour may still be none,
            # since mp_args is a defaultdict.
            colour = mp_args['f']
        else:
            colour = None

        # insert into sql
        await glob.db.execute(
            'INSERT INTO comments '
            '(target_id, target_type, userid, time, comment, colour) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            [target_id, target_type, p.id,
             sttime, comment, colour]
        )

        p.update_latest_activity()
        return # empty resp is fine

    else:
        # invalid action
        return (400, b'Invalid action.')


@domain.route('/web/osu-markasread.php')
@required_args({'u', 'h', 'channel'})
@get_login(name_p='u', pass_p='h')
async def osuMarkAsRead(p: 'Player', conn: Connection) -> Optional[bytes]:
    if not (t_name := unquote(conn.args['channel'])):
        return  # no channel specified

    if t := await glob.players.get_ensure(name=t_name):
        # mark any unread mail from this user as read.
        await glob.db.execute(
            'UPDATE `mail` SET `read` = 1 '
            'WHERE `to_id` = %s AND `from_id` = %s '
            'AND `read` = 0',
            [p.id, t.id]
        )


@domain.route('/web/osu-getseasonal.php')
async def osuSeasonal(conn: Connection) -> Optional[bytes]:
    return orjson.dumps(glob.config.seasonal_bgs)


@domain.route('/web/bancho_connect.php')
async def banchoConnect(conn: Connection) -> Optional[bytes]:
    if 'v' in conn.args:
        # TODO: implement verification..?
        # long term. For now, just send an empty reply
        # so their client immediately attempts login.

        # NOTE: you can actually return an endpoint here
        # for the client to use as a bancho endpoint.
        return

    # TODO: perhaps handle this..?
    NotImplemented

_checkupdates_cache = { # default timeout is 1h, set on request.
    'cuttingedge': {'check': None, 'path': None, 'timeout': 0},
    'stable40': {'check': None, 'path': None, 'timeout': 0},
    'beta40': {'check': None, 'path': None, 'timeout': 0},
    'stable': {'check': None, 'path': None, 'timeout': 0}
}

# NOTE: this will only be triggered when using a server switcher.
@domain.route('/web/check-updates.php')
@required_args({'action', 'stream'})
async def checkUpdates(conn: Connection) -> Optional[bytes]:
    if not glob.has_internet:
        return (503, b'') # requires internet connection

    action = conn.args['action']
    stream = conn.args['stream']

    if action not in ('check', 'path', 'error'):
        return (400, b'') # invalid action

    if stream not in ('cuttingedge', 'stable40', 'beta40', 'stable'):
        return (400, b'') # invalid stream

    if action == 'error':
        # client is just reporting an error updating
        return

    cache = _checkupdates_cache[stream]
    current_time = int(time.time())

    if cache[action] and cache['timeout'] > current_time:
        return cache[action]

    url = 'https://old.ppy.sh/web/check-updates.php'
    async with glob.http.get(url, params=conn.args) as resp:
        if not resp or resp.status != 200:
            return (503, b'') # failed to get data from osu

        result = await resp.read()

    # update the cached result.
    cache[action] = result
    cache['timeout'] = (glob.config.updates_cache_timeout +
                        current_time)

    return result

""" /api/ Handlers """

# Unauthorized (no api key required)
# GET /api/get_player_count: return total registered & online player counts.
# GET /api/get_player_info: return info or stats for a given player.
# GET /api/get_player_status: return a player's current status, if online.
# GET /api/get_player_scores: return a list of best or recent scores for a given player.
# GET /api/get_player_most_played: return a list of maps most played by a given player.
# GET /api/get_map_info: return information about a given beatmap.
# GET /api/get_map_scores: return the best scores for a given beatmap & mode.
# GET /api/get_score_info: return information about a given score.
# GET /api/get_replay: return the file for a given replay (with or without headers).
# GET /api/get_match: return information for a given multiplayer match.
# GET /api/calculate_pp: calculate & return pp for a given beatmap.

# Authorized (requires valid api key, passed as 'Authorization' header)
# NOTE: authenticated handlers may have privilege requirements.

# Authorized (requires valid api key, passed as 'Authorization' header)
# NOTE: authenticated handlers may have privilege requirements.

# [Normal]
# GET /api/calculate_pp: calculate & return pp for a given beatmap.
# POST/PUT /api/set_avatar: Update the tokenholder's avatar to a given file.

# TODO handlers
# GET /api/get_friends: return a list of the player's friends.
# POST/PUT /api/set_player_info: update user information (updates whatever received).

JSON = orjson.dumps
DATETIME_OFFSET = 0x89F7FF5F7B58000

@domain.route('/api/get_player_count')
async def api_get_player_count(conn: Connection) -> Optional[bytes]:
    """Get the current amount of online players."""
    conn.resp_headers['Content-Type'] = f'application/json'
    # TODO: perhaps add peak(s)? (24h, 5d, 3w, etc.)
    # NOTE: -1 is for the bot, and will have to change
    # if we ever make some sort of bot creation system.
    total_users = (await glob.db.fetch(
        'SELECT COUNT(*) FROM users', _dict=False
    ))[0]

    return JSON({
        'status': 'success',
        'counts': {
            'online': len(glob.players.unrestricted) - 1,
            'total': total_users
        }
    })

@domain.route('/api/get_player_info')
async def api_get_player_info(conn: Connection) -> Optional[bytes]:
    """Return information about a given player."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if 'name' not in conn.args and 'id' not in conn.args:
        return (400, JSON({'status': 'Must provide either id or name!'}))

    if (
        'scope' not in conn.args or
        conn.args['scope'] not in ('info', 'stats', 'all')
    ):
        return (400, JSON({'status': 'Must provide scope (info/stats/all).'}))

    if 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Invalid player id.'}))

        pid = conn.args['id']
    else:
        if not 2 <= len(name := unquote(conn.args['name'])) < 16:
            return (400, JSON({'status': 'Invalid player name.'}))

        # get their id from username.
        pid = await glob.db.fetch(
            'SELECT id FROM users '
            'WHERE safe_name = %s',
            [name]
        )

        if not pid:
            return (404, JSON({'status': 'Player not found.'}))

        pid = pid['id']

    api_data = {}

    if conn.args['scope'] in ('info', 'all'): # user info
        res = await glob.db.fetch(
            'SELECT id, name, safe_name, '
            'priv, country, silence_end ' # silence_end public?
            'FROM users WHERE id = %s',
            [pid]
        )

        if not res:
            return (404, JSON({'status': 'Player not found'}))

        api_data |= res

    if conn.args['scope'] in ('stats', 'all'): # user stats
        # get all regular stats
        res = await glob.db.fetch(
            'SELECT stats.*, users.country, users.creation_time, users.latest_activity FROM stats '
            'INNER JOIN users ON users.id = stats.id '
            'WHERE stats.id = %s', [pid]
        )

        for mods in ('vn', 'rx', 'ap'):
            for mode in ('std', 'taiko', 'catch', 'mania'):
                firstplaces = await glob.db.fetch(f'SELECT COUNT(scores_{mods}.id) AS firstplaces FROM scores_{mods} WHERE NOT EXISTS (SELECT 1 FROM scores_{mods} AS leaderboard WHERE leaderboard.score > scores_{mods}.score AND leaderboard.map_md5 = scores_{mods}.map_md5 AND leaderboard.status = 2) AND scores_{mods}.status = 2 AND scores_{mods}.userid = {pid};')
                watched = await glob.db.fetch(f'SELECT CAST(SUM(scores_{mods}.watched) AS UNSIGNED) AS watched FROM scores_{mods} WHERE userid = {pid}')
                res.update({f"firstplaces_{mods}_{mode}":firstplaces.get('firstplaces')})
                res.update({f"watched_{mods}_{mode}":watched.get('watched')})
                pp = res.get(f'pp_{mods}_{mode}')
                if pp is not None:
                    rank = await glob.db.fetch(f'SELECT COUNT(*)+1 AS c FROM stats LEFT JOIN users USING(id) WHERE stats.pp_{mods}_{mode} > {pp} AND users.priv & 1')
                    res.update({f"rank_{mods}_{mode}":rank['c']})

                    crank = await glob.db.fetch(f'SELECT COUNT(*)+1 AS c FROM stats LEFT JOIN users USING(id) WHERE stats.pp_{mods}_{mode} > {pp} AND users.priv & 1 AND users.country = "{res["country"]}"')
                    res.update({f"crank_{mods}_{mode}":crank['c']})

        if not res:
            return (404, JSON({'status': 'Player not found'}))

        api_data |= res

    return orjson.dumps({'status': 'success', 'player': api_data})

@domain.route('/api/get_player_status')
async def api_get_player_status(conn: Connection) -> Optional[bytes]:
    """Return a players current status, if they are online."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if 'id' in conn.args:
        pid = conn.args['id']
        if not pid.isdecimal():
            return (400, JSON({'status': 'Invalid player id.'}))
        # get player by id
        p = glob.players.get(id=int(pid))
    elif 'name' in conn.args:
        name = unquote(conn.args['name'])
        if not 2 <= len(name) < 16:
            return (400, JSON({'status': 'Invalid player name.'}))

        # get player by name
        p = glob.players.get(name=name)
    else:
        return (400, JSON({'status': 'Must provide either id or name!'}))

    if not p:
        # no such player online, return their last seen time
        res = await glob.db.fetch('SELECT latest_activity FROM users WHERE id = %s', [pid])
        if not res:
            return (404, JSON({'status': 'Player not found.'}))

        return JSON({
            'status': 'success',
            'player_status': {
                'online': False,
                'last_seen': res['latest_activity']
            }
        })

    if p.status.map_md5:
        bmap = await Beatmap.from_md5(p.status.map_md5)
    else:
        bmap = None

    return JSON({
        'status': 'success',
        'player_status': {
            'online': True,
            'login_time': p.login_time,
            'status': {
                'action': int(p.status.action),
                'info_text': p.status.info_text,
                'mode': int(p.status.mode),
                'mods': int(p.status.mods),
                'beatmap': {
                    'md5': bmap.md5,
                    'id': bmap.id,
                    'set_id': bmap.set_id,
                    'artist': bmap.artist,
                    'title': bmap.title,
                    'version': bmap.version,
                    'creator': bmap.creator,
                    'last_update': bmap.last_update,
                    'total_length': bmap.total_length,
                    'max_combo': bmap.max_combo,
                    'status': bmap.status,
                    'plays': bmap.plays,
                    'passes': bmap.passes,
                    'mode': bmap.mode,
                    'bpm': bmap.bpm,
                    'cs': bmap.cs,
                    'od': bmap.od,
                    'ar': bmap.ar,
                    'hp': bmap.hp,
                    'diff': bmap.diff
                } if bmap else None
            }
        }
    })

@domain.route('/api/get_player_scores')
async def api_get_player_scores(conn: Connection) -> Optional[bytes]:
    """Return a list of a given user's recent/best scores."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Invalid player id.'}))
        p = await glob.players.get_ensure(id=int(conn.args['id']))
    elif 'name' in conn.args:
        if not 0 < len(conn.args['name']) <= 16:
            return (400, JSON({'status': 'Invalid player name.'}))
        p = await glob.players.get_ensure(name=conn.args['name'])
    else:
        return (400, JSON({'status': 'Must provide either id or name.'}))

    if not p:
        return (404, JSON({'status': 'Player not found.'}))

    # parse args (scope, mode, mods, limit)

    if not (
        'scope' in conn.args and
        conn.args['scope'] in ('recent', 'best')
    ):
        return (400, JSON({'status': 'Must provide valid scope (recent/best).'}))

    scope = conn.args['scope']

    if (mode_arg := conn.args.get('mode', None)) is not None:
        if not (
            mode_arg.isdecimal() and
            0 <= (mode := int(mode_arg)) <= 7
        ):
            return (400, JSON({'status': 'Invalid mode.'}))

        mode = GameMode(mode)
    else:
        mode = GameMode.vn_std

    if (mods_arg := conn.args.get('mods', None)) is not None:
        if mods_arg[0] in ('~', '='): # weak/strong equality
            strong_equality = mods_arg[0] == '='
            mods_arg = mods_arg[1:]
        else: # use strong as default
            strong_equality = True

        if mods_arg.isdecimal():
            # parse from int form
            mods = Mods(int(mods_arg))
        else:
            # parse from string form
            mods = Mods.from_modstr(mods_arg)
    else:
        mods = None

    if (limit_arg := conn.args.get('limit', None)) is not None:
        if not (
            limit_arg.isdecimal() and
            0 < (limit := int(limit_arg)) <= 100
        ):
            return (400, JSON({'status': 'Invalid limit.'}))
    else:
        limit = 25

    if (bm := conn.args.get('bm', None)) is not None:
        bm = bm
    else:
        bm = None

    # build sql query & fetch info

    if (table := conn.args.get('table', None)) is not None:
        query = [
            'SELECT id, map_md5, score, pp, acc, max_combo, '
            'mods, n300, n100, n50, nmiss, ngeki, nkatu, grade, '
            'status, mode, play_time, time_elapsed, perfect '
            f'FROM scores_{table} WHERE userid = %s AND mode = %s'
        ]
    else:
        query = [
            'SELECT id, map_md5, score, pp, acc, max_combo, '
            'mods, n300, n100, n50, nmiss, ngeki, nkatu, grade, '
            'status, mode, play_time, time_elapsed, perfect '
            f'FROM {mode.scores_table} WHERE userid = %s AND mode = %s'
        ]

    params = [p.id, mode.as_vanilla]

    if mods is not None:
        if strong_equality:
            query.append('AND mods & %s = %s')
            params.extend((mods, mods))
        else:
            query.append('AND mods & %s != 0')
            params.append(mods)

    if bm is not None:
        query.append('AND map_md5 = %s')
        params.append(bm)

    if scope == 'best':
        query.append('AND grade != "F" AND status = 2')

    sort = 'pp' if scope == 'best' else 'play_time'

    query.append(f'ORDER BY {sort} DESC LIMIT %s')
    params.append(limit)

    # fetch & return info from sql
    res = await glob.db.fetchall(' '.join(query), params)

    for row in res:
        bmap = await Beatmap.from_md5(row.pop('map_md5'))
        row['beatmap'] = {
            'md5': bmap.md5,
            'id': bmap.id,
            'set_id': bmap.set_id,
            'artist': bmap.artist,
            'title': bmap.title,
            'version': bmap.version,
            'creator': bmap.creator,
            'last_update': bmap.last_update,
            'total_length': bmap.total_length,
            'max_combo': bmap.max_combo,
            'status': bmap.status,
            'plays': bmap.plays,
            'passes': bmap.passes,
            'mode': bmap.mode,
            'bpm': bmap.bpm,
            'cs': bmap.cs,
            'od': bmap.od,
            'ar': bmap.ar,
            'hp': bmap.hp,
            'diff': bmap.diff
        }

    player_info = {
        'id': p.id,
        'name': p.name,
        'clan': {
            'id': p.clan.id,
            'name': p.clan.name,
            'tag': p.clan.tag
        } if p.clan else None
    }

    return JSON({'status': 'success', 'scores': res, 'player': player_info})

@domain.route('/api/get_player_most_played')
async def api_get_player_most_played(conn: Connection) -> Optional[bytes]:
    """Return the most played beatmaps of a given player."""
    # NOTE: this will almost certainly not scale well, lol.
    conn.resp_headers['Content-Type'] = f'application/json'

    if 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Invalid player id.'}))
        p = await glob.players.get_ensure(id=int(conn.args['id']))
    elif 'name' in conn.args:
        if not 0 < len(conn.args['name']) <= 16:
            return (400, JSON({'status': 'Invalid player name.'}))
        p = await glob.players.get_ensure(name=conn.args['name'])
    else:
        return (400, JSON({'status': 'Must provide either id or name.'}))

    if not p:
        return (404, JSON({'status': 'Player not found.'}))

    # parse args (mode, limit)

    if (mode_arg := conn.args.get('mode', None)) is not None:
        if not (
            mode_arg.isdecimal() and
            0 <= (mode := int(mode_arg)) <= 7
        ):
            return (400, JSON({'status': 'Invalid mode.'}))

        mode = GameMode(mode)
    else:
        mode = GameMode.vn_std

    if (limit_arg := conn.args.get('limit', None)) is not None:
        if not (
            limit_arg.isdecimal() and
            0 < (limit := int(limit_arg)) <= 100
        ):
            return (400, JSON({'status': 'Invalid limit.'}))
    else:
        limit = 25

    # fetch & return info from sql
    res = await glob.db.fetchall(
        'SELECT m.md5, m.id, m.set_id, m.status, '
        'm.artist, m.title, m.version, m.creator, COUNT(*) plays '
        f'FROM {mode.scores_table} s '
        'INNER JOIN maps m ON m.md5 = s.map_md5 '
        'WHERE s.userid = %s '
        'AND s.mode = %s '
        'GROUP BY s.map_md5 '
        'ORDER BY plays DESC '
        'LIMIT %s',
        [p.id, mode.as_vanilla, limit]
    )

    return JSON({'status': 'success', 'maps': res})

@domain.route('/api/get_map_info')
async def api_get_map_info(conn: Connection) -> Optional[bytes]:
    """Return information about a given beatmap."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Invalid map id.'}))
        bmap = await Beatmap.from_bid(int(conn.args['id']))
    elif 'md5' in conn.args:
        if len(conn.args['md5']) != 32:
            return (400, JSON({'status': 'Invalid map md5.'}))
        bmap = await Beatmap.from_md5(conn.args['md5'])
    else:
        return (400, JSON({'status': 'Must provide either id or md5!'}))

    if not bmap:
        return (404, JSON({'status': 'Map not found.'}))

    return JSON({
        'status': 'success',
        'map': {
            'md5': bmap.md5,
            'id': bmap.id,
            'set_id': bmap.set_id,
            'artist': bmap.artist,
            'title': bmap.title,
            'version': bmap.version,
            'creator': bmap.creator,
            'last_update': bmap.last_update,
            'total_length': bmap.total_length,
            'max_combo': bmap.max_combo,
            'status': bmap.status,
            'plays': bmap.plays,
            'passes': bmap.passes,
            'mode': bmap.mode,
            'bpm': bmap.bpm,
            'cs': bmap.cs,
            'od': bmap.od,
            'ar': bmap.ar,
            'hp': bmap.hp,
            'diff': bmap.diff
        }
    })

@domain.route('/api/get_map_scores')
async def api_get_map_scores(conn: Connection) -> Optional[bytes]:
    """Return the top n scores on a given beatmap."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Invalid map id.'}))
        bmap = await Beatmap.from_bid(int(conn.args['id']))
    elif 'md5' in conn.args:
        if len(conn.args['md5']) != 32:
            return (400, JSON({'status': 'Invalid map md5.'}))
        bmap = await Beatmap.from_md5(conn.args['md5'])
    else:
        return (400, JSON({'status': 'Must provide either id or md5!'}))

    if not bmap:
        return (404, JSON({'status': 'Map not found.'}))

    # parse args (scope, mode, mods, limit)

    if (
        'scope' not in conn.args or
        conn.args['scope'] not in ('recent', 'best')
    ):
        return (400, JSON({'status': 'Must provide valid scope (recent/best).'}))

    scope = conn.args['scope']

    if (mode_arg := conn.args.get('mode', None)) is not None:
        if not (
            mode_arg.isdecimal() and
            0 <= (mode := int(mode_arg)) <= 7
        ):
            return (400, JSON({'status': 'Invalid mode.'}))

        mode = GameMode(mode)
    else:
        mode = GameMode.vn_std

    if (mods_arg := conn.args.get('mods', None)) is not None:
        if mods_arg[0] in ('~', '='): # weak/strong equality
            strong_equality = mods_arg[0] == '='
            mods_arg = mods_arg[1:]
        else: # use strong as default
            strong_equality = True

        if mods_arg.isdecimal():
            # parse from int form
            mods = Mods(int(mods_arg))
        else:
            # parse from string form
            mods = Mods.from_modstr(mods_arg)
    else:
        mods = None

    if (limit_arg := conn.args.get('limit', None)) is not None:
        if not (
            limit_arg.isdecimal() and
            0 < (limit := int(limit_arg)) <= 100
        ):
            return (400, JSON({'status': 'Invalid limit.'}))
    else:
        limit = 50

    # NOTE: userid will eventually become player_id,
    # along with everywhere else in the codebase.
    query = [
        'SELECT s.map_md5, s.score, s.pp, s.acc, s.max_combo, s.mods, '
        's.n300, s.n100, s.n50, s.nmiss, s.ngeki, s.nkatu, s.grade, s.status, '
        's.mode, s.play_time, s.time_elapsed, s.userid, s.perfect, '
        'u.name player_name, '
        'c.id clan_id, c.name clan_name, c.tag clan_tag '
        f'FROM {mode.scores_table} s '
        'INNER JOIN users u ON u.id = s.userid '
        'LEFT JOIN clans c ON c.id = u.clan_id '
        'WHERE s.map_md5 = %s AND s.mode = %s AND s.status = 2'
    ]
    params = [bmap.md5, mode.as_vanilla]

    if mods is not None:
        if strong_equality:
            query.append('AND mods & %s = %s')
            params.extend((mods, mods))
        else:
            query.append('AND mods & %s != 0')
            params.append(mods)

    # unlike /api/get_player_scores, we'll sort by score/pp depending
    # on the mode played, since we want to replicated leaderboards.
    if scope == 'best':
        sort = 'pp' if mode >= GameMode.rx_std else 'score'
    else: # recent
        sort = 'play_time'

    if (user := conn.args.get('user', None)) is not None:
        if not user.isdecimal():
            if not await glob.db.fetch("SELECT 1 FROM users WHERE safe_name = %s", [user]):
                return (400, JSON({'status': 'Invalid user.'}))
            else:
                query.append('AND u.name = %s')
                params.append(user)
        elif not await glob.db.fetch("SELECT 1 FROM users WHERE id = %s", [user]):
            return (400, JSON({'status': 'Invalid user.'}))
        else:
            query.append('AND s.userid = %s')
            params.append(user)

    query.append(f'ORDER BY {sort} DESC LIMIT %s')
    params.append(limit)

    res = await glob.db.fetchall(' '.join(query), params)
    return JSON({'status': 'success', 'scores': res})

@domain.route('/api/get_score_info')
async def api_get_score_info(conn: Connection) -> Optional[bytes]:
    """Return information about a given score."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if not (
        'id' in conn.args and
        conn.args['id'].isdecimal()
    ):
        return (400, JSON({'status': 'Must provide score id.'}))

    score_id = int(conn.args['id'])

    if SCOREID_BORDERS[0] > score_id >= 1:
        scores_table = 'scores_vn'
    elif SCOREID_BORDERS[1] > score_id >= SCOREID_BORDERS[0]:
        scores_table = 'scores_rx'
    elif SCOREID_BORDERS[2] > score_id >= SCOREID_BORDERS[1]:
        scores_table = 'scores_ap'
    else:
        return (400, JSON({'status': 'Invalid score id.'}))

    res = await glob.db.fetch(
        'SELECT map_md5, score, pp, acc, max_combo, mods, '
        'n300, n100, n50, nmiss, ngeki, nkatu, grade, status, '
        'mode, play_time, time_elapsed, perfect '
        f'FROM {scores_table} '
        'WHERE id = %s',
        [score_id]
    )

    if not res:
        return (404, JSON({'status': 'Score not found.'}))

    return JSON({'status': 'success', 'score': res})

@domain.route('/api/get_replay')
async def api_get_replay(conn: Connection) -> Optional[bytes]:
    """Return a given replay (including headers)."""
    conn.resp_headers['Content-Type'] = f'application/json'
    if not (
        'id' in conn.args and
        conn.args['id'].isdecimal()
    ):
        return (400, JSON({'status': 'Must provide score id.'}))

    score_id = int(conn.args['id'])

    if SCOREID_BORDERS[0] > score_id >= 1:
        scores_table = 'scores_vn'
    elif SCOREID_BORDERS[1] > score_id >= SCOREID_BORDERS[0]:
        scores_table = 'scores_rx'
    elif SCOREID_BORDERS[2] > score_id >= SCOREID_BORDERS[1]:
        scores_table = 'scores_ap'
    else:
        return (400, JSON({'status': 'Invalid score id.'}))

    # fetch replay file & make sure it exists
    replay_file = REPLAYS_PATH / f'{score_id}.osr'
    if not replay_file.exists():
        return (404, JSON({'status': 'Replay not found.'}))

    # read replay frames from file
    raw_replay = replay_file.read_bytes()

    if (
        'include_headers' in conn.args and
        conn.args['include_headers'].lower() == 'false'
    ):
        if len(str(struct.unpack('<i', raw_replay[1:5])[0])) == 8:
            parsed = parse_replay_file(replay_file)
            return parsed.lzma_string
        else:
            return raw_replay

    if len(str(struct.unpack('<i', raw_replay[1:5])[0])) == 8:
        # add replay headers from sql
        res = await glob.db.fetch(
            'SELECT u.name username, m.md5 map_md5, '
            'm.artist, m.title, m.version, '
            's.mode, s.n300, s.n100, s.n50, s.ngeki, '
            's.nkatu, s.nmiss, s.score, s.max_combo, '
            's.perfect, s.mods, s.play_time '
            f'FROM {scores_table} s '
            'INNER JOIN users u ON u.id = s.userid '
            'INNER JOIN maps m ON m.md5 = s.map_md5 '
            'WHERE s.id = %s',
            [score_id]
        )

        if not res:
            # score not found in sql
            return (404, b'Score not found.') # but replay was? lol

        # send data back to the client
        conn.resp_headers['Content-Type'] = 'application/octet-stream'
        conn.resp_headers['Content-Description'] = 'File Transfer'
        conn.resp_headers['Content-Disposition'] = (
            'attachment; filename="{username} - '
            '{artist} - {title} [{version}] '
            '({play_time:%Y-%m-%d}).osr"'
        ).format(**res)

        return replay_file.read_bytes()
    else:
        # add replay headers from sql
        # TODO: osu_version & life graph in scores tables?
        res = await glob.db.fetch(
            'SELECT u.name username, m.md5 map_md5, '
            'm.artist, m.title, m.version, '
            's.mode, s.n300, s.n100, s.n50, s.ngeki, '
            's.nkatu, s.nmiss, s.score, s.max_combo, '
            's.perfect, s.mods, s.play_time '
            f'FROM {scores_table} s '
            'INNER JOIN users u ON u.id = s.userid '
            'INNER JOIN maps m ON m.md5 = s.map_md5 '
            'WHERE s.id = %s',
            [score_id]
        )

        if not res:
            # score not found in sql
            return (404, b'Score not found.') # but replay was? lol

        # generate the replay's hash
        replay_md5 = hashlib.md5(
            '{}p{}o{}o{}t{}a{}r{}e{}y{}o{}u{}{}{}'.format(
                res['n100'] + res['n300'], res['n50'],
                res['ngeki'], res['nkatu'], res['nmiss'],
                res['map_md5'], res['max_combo'],
                str(res['perfect'] == 1),
                res['username'], res['score'], 0, # TODO: rank
                res['mods'], 'True' # TODO: ??
            ).encode()
        ).hexdigest()

        # create a buffer to construct the replay output
        buf = bytearray()

        # pack first section of headers.
        buf += struct.pack('<Bi', res['mode'], 20200207) # TODO: osuver
        buf += packets.write_string(res['map_md5'])
        buf += packets.write_string(res['username'])
        buf += packets.write_string(replay_md5)
        buf += struct.pack(
            '<hhhhhhihBi',
            res['n300'], res['n100'], res['n50'],
            res['ngeki'], res['nkatu'], res['nmiss'],
            res['score'], res['max_combo'], res['perfect'],
            res['mods']
        )
        buf += b'\x00' # TODO: hp graph

        timestamp = int(res['play_time'].timestamp() * 1e7)
        buf += struct.pack('<q', timestamp + DATETIME_OFFSET)

        # pack the raw replay data into the buffer
        buf += struct.pack('<i', len(raw_replay))
        buf += raw_replay

        # pack additional info info buffer.
        buf += struct.pack('<q', score_id)

        # NOTE: target practice sends extra mods, but
        # can't submit scores so should not be a problem.

        # send data back to the client
        conn.resp_headers['Content-Type'] = 'application/octet-stream'
        conn.resp_headers['Content-Description'] = 'File Transfer'
        conn.resp_headers['Content-Disposition'] = (
            'attachment; filename="{username} - '
            '{artist} - {title} [{version}] '
            '({play_time:%Y-%m-%d}).osr"'
        ).format(**res)

        return bytes(buf)

@domain.route('/api/get_match')
async def api_get_match(conn: Connection) -> Optional[bytes]:
    """Return information of a given multiplayer match."""
    conn.resp_headers['Content-Type'] = f'application/json'
    # TODO: eventually, this should contain recent score info.
    if not (
        'id' in conn.args and
        conn.args['id'].isdecimal() and
        0 <= (match_id := int(conn.args['id'])) < 64
    ):
        return (400, JSON({'status': 'Must provide valid match id.'}))

    if not (match := glob.matches[match_id]):
        return (404, JSON({'status': 'Match not found.'}))

    return JSON({
        'status': 'success',
        'match': {
            'name': match.name,
            'mode': match.mode.as_vanilla,
            'mods': int(match.mods),
            'seed': match.seed,
            'host': {
                'id': match.host.id,
                'name': match.host.name
            },
            'refs': [{'id': p.id, 'name': p.name} for p in match.refs],
            'in_progress': match.in_progress,
            'is_scrimming': match.is_scrimming,
            'map': {
                'id': match.map_id,
                'md5': match.map_md5,
                'name': match.map_name
            },
            'active_slots': {
                str(idx): {
                    'loaded': slot.loaded,
                    'mods': int(slot.mods),
                    'player': {
                        'id': slot.player.id,
                        'name': slot.player.name
                    },
                    'skipped': slot.skipped,
                    'status': int(slot.status),
                    'team': int(slot.team)
                } for idx, slot in enumerate(match.slots) if slot.player
            }
        }
    })

@domain.route('/api/get_player_bydiscord')
async def get_player_bydiscord(conn: Connection) -> Optional[bytes]:
    if not 'id' in conn.args or not 'secret' in conn.args:
        return b'lol'

    if conn.args['secret'] != glob.config.discord_secret:
        return b'2lol'

    res = await glob.db.fetch('SELECT * FROM discord WHERE discord_id = %s', [conn.args['id']])

    return JSON(res)

def requires_api_key(f: Callable) -> Callable:
    @wraps(f)
    async def wrapper(conn: Connection) -> Optional[bytes]:
        conn.resp_headers['Content-Type'] = f'application/json'
        if 'Authorization' not in conn.headers:
            return (400, JSON({'status': 'Must provide authorization token.'}))

        api_key = conn.headers['Authorization']

        if api_key not in glob.api_keys:
            return (401, JSON({'status': 'Unknown authorization token.'}))

        # get player from api token
        player_id = glob.api_keys[api_key]
        p = await glob.players.get_ensure(id=player_id)

        return await f(conn, p)
    return wrapper

# NOTE: `Content-Type = application/json` is applied in the above decorator
#                                         for the following api handlers.

# TODO: mania support (and ctb later)
@domain.route('/api/calculate_pp')
@requires_api_key
async def api_calculate_pp(conn: Connection, p: 'Player') -> Optional[bytes]:
    """Calculate and return pp & sr for a given map."""
    if 'md5' in conn.args:
        # get id from md5
        bmap = await Beatmap.from_md5(md5=conn.args['md5'])
    elif 'id' in conn.args:
        if not conn.args['id'].isdecimal():
            return (400, JSON({'status': 'Failed: invalid map id'}))

        bmap = await Beatmap.from_md5(md5=conn.args['md5'])
    else:
        return (400, JSON({'status': 'Failed: Must provide map md5 or id'}))

    if not bmap:
        return JSON({'status': 'Failed: map not found'})

    pp_kwargs = {}
    valid_kwargs = (
        ('mods', int),
        ('combo', int),
        ('nmiss', int),
        ('mode_vn', int),
        ('acc', float)
    )

    # ignore any invalid args
    for n, t in valid_kwargs:
        if n in conn.args:
            val = conn.args[n]

            if not _isdecimal(val, _float=t is float):
                continue

            pp_kwargs[n] = t(val)

    if pp_kwargs.get('mode_vn', 0) not in (0, 1):
        return (503, JSON({'status': 'Failed: unsupported mode'}))

    ppcalc = await PPCalculator.from_map(bmap, **pp_kwargs)

    if not ppcalc:
        return JSON({'status': 'Failed: could not retrieve map'})

    pp, sr = await ppcalc.perform()

    return JSON({
        'status': 'Success',
        'pp': pp,
        'sr': sr
    })

@domain.route('/api/set_avatar', methods=['POST', 'PUT'])
@requires_api_key
async def api_set_avatar(conn: Connection, p: 'Player') -> Optional[bytes]:
    """Update the tokenholder's avatar to a given file."""
    if 'avatar' not in conn.files:
        return (400, JSON({'status': 'must provide avatar file.'}))

    ava_file = conn.files['avatar']

    # block files over 4MB
    if len(ava_file) > (4 * 1024 * 1024):
        return (400, JSON({'status': 'avatar file too large (max 4MB).'}))

    if ava_file[6:10] in (b'JFIF', b'Exif'):
        ext = 'jpeg'
    elif ava_file.startswith(b'\211PNG\r\n\032\n'):
        ext = 'png'
    else:
        return (400, JSON({'status': 'invalid file type.'}))

    # write to the avatar file
    (AVATARS_PATH / f'{p.id}.{ext}').write_bytes(ava_file)
    return JSON({'status': 'success.'})

""" Misc handlers """

if glob.config.redirect_osu_urls:
    # NOTE: this will likely be removed with the addition of a frontend.
    @domain.route({re.compile(r'^/beatmapsets/\d{1,10}(?:/discussion)?/?$'),
                   re.compile(r'^/beatmaps/\d{1,10}/?'),
                   re.compile(r'^/community/forums/topics/\d{1,10}/?$')})
    async def osu_redirects(conn: Connection) -> Optional[bytes]:
        """Redirect some common url's the client uses to osu!."""
        conn.resp_headers['Location'] = f'https://osu.ppy.sh{conn.path}'
        return (301, b'')

@domain.route(re.compile(r'^/ss/[a-zA-Z0-9-_]{8}\.(png|jpeg)$'))
async def get_screenshot(conn: Connection) -> Optional[bytes]:
    """Serve a screenshot from the server, by filename."""
    if len(conn.path) not in (16, 17):
        return (400, b'Invalid request.')

    path = SCREENSHOTS_PATH / conn.path[4:]

    if not path.exists():
        return (404, JSON({'status': 'Screenshot not found.'}))

    return path.read_bytes()

@domain.route(re.compile(r'^/d/\d{1,10}n?$'))
async def get_osz(conn: Connection) -> Optional[bytes]:
    """Handle a map download request (osu.ppy.sh/d/*)."""
    set_id = conn.path[3:]

    # if no_video := set_id[-1] == 'n':
    #     set_id = set_id[:-1]

    # if USING_CHIMU:
    #     query_str = f'download/{set_id}?n={int(no_video)}'
    # else:
    #     query_str = f'd/{set_id}'

    conn.resp_headers['Location'] = f'https://osu.gatari.pw/d/{set_id}'
    return (301, b'')

@domain.route(re.compile(r'^/web/maps/'))
async def get_updated_beatmap(conn: Connection) -> Optional[bytes]:
    """Send the latest .osu file the server has for a given map."""
    if conn.headers['Host'] == 'osu.ppy.sh':
        # server switcher, use old method
        map_filename = unquote(conn.path[10:])

        if not (res := await glob.db.fetch(
            'SELECT id, md5 '
            'FROM maps '
            'WHERE filename = %s',
            [map_filename]
        )):
            return (404, b'') # map not found in sql

        path = BEATMAPS_PATH / f'{res["id"]}.osu'

        if (
            path.exists() and
            res['md5'] == hashlib.md5(path.read_bytes()).hexdigest()
        ):
            # up to date map found on disk.
            content = path.read_bytes()
        else:
            if not glob.has_internet:
                return (503, b'') # requires internet connection

            # map not found, or out of date; get from osu!
            url = f"https://old.ppy.sh/osu/{res['id']}"

            async with glob.http.get(url) as resp:
                if not resp or resp.status != 200:
                    log(f'Could not find map {path}!', Ansi.LRED)
                    return (404, b'') # couldn't find on osu!'s server

                content = await resp.read()

            # save it to disk for future
            path.write_bytes(content)

        return content
    else:
        # using -devserver, just redirect them to osu
        conn.resp_headers['Location'] = f'https://osu.ppy.sh{conn.path}'
        return (301, b'')


@domain.route('/p/doyoureallywanttoaskpeppy')
async def peppyDMHandler(conn: Connection) -> Optional[bytes]:
    return (
        b"This user's ID is usually peppy's (when on bancho), "
        b"and is blocked from being messaged by the osu! client."
    )

""" ingame registration """


@domain.route('/users', methods=['POST'])
@ratelimit(period=300, max_count=15)  # 15 registrations / 5mins
async def register_account(conn: Connection) -> Optional[bytes]:
    if glob.config.disable_ingame_reg:    
        return (400, b'In-Game Registration is DISABLED Register FROM SITE! ^_^')
    
    mp_args = conn.multipart_args

    name = mp_args['user[username]'].strip()
    email = mp_args['user[user_email]']
    pw_txt = mp_args['user[password]']

    if not all((name, email, pw_txt)) or 'check' not in mp_args:
        return (400, b'Missing required params')

    # ensure all args passed
    # are safe for registration.
    errors = defaultdict(list)

    # Usernames must:
    # - be within 2-15 characters in length
    # - not contain both ' ' and '_', one is fine
    # - not be in the config's `disallowed_names` list
    # - not already be taken by another player
    if not regexes.username.match(name):
        errors['username'].append('Must be 2-15 characters in length.')

    if '_' in name and ' ' in name:
        errors['username'].append('May contain "_" and " ", but not both.')

    if name in glob.config.disallowed_names:
        errors['username'].append('Disallowed username; pick another.')

    if 'username' not in errors:
        await db_cursor.execute('SELECT 1 FROM users WHERE name = %s', [name])
        if db_cursor.rowcount != 0:
            errors['username'].append('Username already taken by another player.')

    # Emails must:
    # - match the regex `^[^@\s]{1,200}@[^@\s\.]{1,30}\.[^@\.\s]{1,24}$`
    # - not already be taken by another player
    if not regexes.email.match(email):
        errors['user_email'].append('Invalid email syntax.')
    else:
        await db_cursor.execute('SELECT 1 FROM users WHERE email = %s', [email])
        if db_cursor.rowcount != 0:
            errors['user_email'].append('Email already taken by another player.')

    # Passwords must:
    # - be within 8-32 characters in length
    # - have more than 3 unique characters
    # - not be in the config's `disallowed_passwords` list
    if not 8 <= len(pw_txt) <= 32:
        errors['password'].append('Must be 8-32 characters in length.')

    if len(set(pw_txt)) <= 3:
        errors['password'].append('Must have more than 3 unique characters.')

    if pw_txt.lower() in glob.config.disallowed_passwords:
        errors['password'].append('That password was deemed too simple.')

    if errors:
        # we have errors to send back, send them back delimited by newlines.
        errors = {k: ['\n'.join(v)] for k, v in errors.items()}
        errors_full = {'form_error': {'user': errors}}
        return (400, orjson.dumps(errors_full))

    if mp_args['check'] == '0':
        # the client isn't just checking values,
        # they want to register the account now.
        # make the md5 & bcrypt the md5 for sql.
        async with glob.players._lock:
            pw_md5 = hashlib.md5(pw_txt.encode()).hexdigest().encode()
            pw_bcrypt = bcrypt.hashpw(pw_md5, bcrypt.gensalt())
            glob.cache['bcrypt'][pw_bcrypt] = pw_md5  # cache result for login

            safe_name = name.lower().replace(' ', '_')

            if 'CF-IPCountry' in conn.headers:
                # best case, dev has enabled ip geolocation in the
                # network tab of cloudflare, so it sends the iso code.
                country = conn.headers['CF-IPCountry']
            else:
                # backup method, get the user's ip and
                # do a db lookup to get their country.
                if 'CF-Connecting-IP' in conn.headers:
                    ip = conn.headers['CF-Connecting-IP']
                else:
                    # if the request has been forwarded, get the origin
                    forwards = conn.headers['X-Forwarded-For'].split(',')
                    if len(forwards) != 1:
                        ip = forwards[0]
                    else:
                        ip = conn.headers['X-Real-IP']

                if ip != '127.0.0.1':
                    if glob.geoloc_db is not None:
                        # decent case, dev has downloaded a geoloc db from
                        # maxmind, so we can do a local db lookup. (~1-5ms)
                        # https://www.maxmind.com/en/home
                        geoloc = utils.misc.fetch_geoloc_db(ip)
                    else:
                        # worst case, we must do an external db lookup
                        # using a public api. (depends, `ping ip-api.com`)
                        geoloc = await utils.misc.fetch_geoloc_web(ip)

                    country = geoloc['country']
                else:
                    # localhost, unknown country
                    country = 'XX'

            # add to `users` table.
            await db_cursor.execute(
                'INSERT INTO users '
                '(name, safe_name, email, pw_bcrypt, country, creation_time, latest_activity) '
                'VALUES (%s, %s, %s, %s, %s, UNIX_TIMESTAMP(), UNIX_TIMESTAMP())',
                [name, safe_name, email, pw_bcrypt, country]
            )
            user_id = db_cursor.lastrowid

            # add to `stats` table.
            await db_cursor.execute(
                'INSERT INTO stats '
                '(id) VALUES (%s)',
                [user_id]
            )

        if glob.datadog:
            glob.datadog.increment('gulag.registrations')

        log(f'<{name} ({user_id})> has registered!', Ansi.LGREEN)

    return b'ok'  # success

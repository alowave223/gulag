# -*- coding: utf-8 -*-

import re
from pathlib import Path
from typing import Optional

from cmyui.web import Connection
from cmyui.web import Domain

from objects import glob

""" ava: avatar server (for both ingame & external) """

domain = Domain(f'a.{glob.config.domain}')

AVATARS_PATH = Path.cwd() / '.data/avatars'
DEFAULT_AVATAR = AVATARS_PATH / 'default.jpg'
DEFAULT_BACKGROUND = AVATARS_PATH / 'defaultbg.jpg'
BDEFAULT_BACKGROUND = AVATARS_PATH / 'bdefaultbg.png'
@domain.route(re.compile(r'^/(?:-|--)?|(?:\d{1,10}(?:\.(?:jpg|jpeg|png))?|favicon\.ico)?$'))
async def get_avatar(conn: Connection) -> Optional[bytes]:
    filename = conn.path[1:]

    if '.' in filename:
        # user id & file extension provided
        path = AVATARS_PATH / filename
        if not path.exists():
            if '--' in conn.path:
                path = DEFAULT_BACKGROUND
            elif 'b' in conn.path:
                path = BDEFAULT_BACKGROUND
            else:
                # no file exists
                path = DEFAULT_AVATAR
    elif filename not in ('', 'favicon.ico'):
        # user id provided - determine file extension
        for ext in ('jpg', 'jpeg', 'png'):
            path = AVATARS_PATH / f'{filename}.{ext}'
            if path.exists():
                break
        else:
            if '--' in conn.path:
                path = DEFAULT_BACKGROUND
            elif 'b' in conn.path:
                path = BDEFAULT_BACKGROUND
            else:
                # no file exists
                path = DEFAULT_AVATAR
    else:
        # empty path or favicon, serve default avatar
        path = DEFAULT_AVATAR

    ext = 'png' if path.suffix == '.png' else 'jpeg'
    conn.resp_headers['Content-Type'] = f'image/{ext}'
    return path.read_bytes()

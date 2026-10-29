"""Explicit private image saves and original-image replies, never public export."""
import asyncio
from collections import OrderedDict
from contextlib import contextmanager
import copy
import hashlib
from pathlib import Path
import re
import sqlite3
import time
from PIL import Image as PILImage

SAVE = re.compile(r'(?:这(?:个|张)?(?:表情包|表情|图|图片)(?:你)?(?:拿去用|收下|存下|收藏|保存)(?:吧|哦|啦)?|(?:把)?这(?:个|张)?(?:表情包|表情|图|图片)(?:收下|存下|收藏|保存)(?:吧|哦|啦)?)')
SEND = re.compile(r'(?:把)?(?:刚才(?:那|这)?张|这张|原图|收藏的(?:那张)?)(?:表情包|表情|图|图片)?(?:发|发回|还)(?:给)?我(?:看看|看下|看|一下)?(?:吧|哦)?|发给我看看')
DELETE = re.compile(r'(?:删除|清空)(?:我(?:的)?)?图片收藏')
INDIRECT = re.compile(r'他说|她说|别人|假如|假设|比如|举例|转述|翻译|台词|[“”「」『』"`]|[?？]')
FORMATS = {'PNG':'.png','JPEG':'.jpg','GIF':'.gif','WEBP':'.webp'}
MAX_BYTES = 6*1024*1024


def media_intent(text, previous=''):
    value = str(text or '').strip().strip('。！!，, ')
    if INDIRECT.search(value):return None
    if SAVE.fullmatch(value):return 'save'
    if DELETE.fullmatch(value):return 'forget'
    if SEND.fullmatch(value) and (value!='发给我看看' or re.search(r'表情|图片|原图|这张|收藏|拿去用|收下',previous)):
        return 'send'
    return None


class ImageCollection:
    def __init__(self, database, directory, temp_root, clock=time.time):
        self.database, self.directory = Path(database), Path(directory)
        self.temp_root, self.clock = Path(temp_root).resolve(), clock
        self.pending = OrderedDict()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS chat_images(
                actor TEXT,origin TEXT,digest TEXT,path TEXT NOT NULL,at REAL NOT NULL,
                PRIMARY KEY(actor,origin,digest))''')
        self.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=3)
        try:
            with db:yield db
        finally:db.close()

    def capture(self, actor, origin, images):
        self.pending[(str(actor),origin)] = (copy.copy(images[0]) if len(images)==1 else None,self.clock())
        self.pending.move_to_end((str(actor),origin))
        while len(self.pending)>128:self.pending.popitem(last=False)

    @staticmethod
    def validate(path):
        if not 0<path.stat().st_size<=MAX_BYTES:raise ValueError('图片大小超出收藏限制，这次没有保存。')
        with PILImage.open(path) as image:
            if image.format not in FORMATS or image.width*image.height>24_000_000:
                raise ValueError('这张图片格式或尺寸暂不适合收藏。')
            suffix = FORMATS[image.format]
            image.verify()
        return path.read_bytes(),suffix

    async def source(self, actor, origin):
        cached = self.pending.get((str(actor),origin))
        if cached and self.clock()-cached[1]>300:
            raise ValueError('刚才那张图片的临时记录已过期，请重新发一次，避免拿错旧图。')
        if cached and self.clock()-cached[1]<=300:
            if cached[0] is None:raise ValueError('刚才有多张图片，我还不能确定你指哪张。')
            path = Path(await asyncio.wait_for(cached[0].convert_to_file_path(),timeout=15)).resolve()
            if self.temp_root not in path.parents:
                raise ValueError('这次没取得可用的原图，请把那张图片再发一次。')
            return await asyncio.to_thread(self.validate,path)
        with self.connect() as db:
            row = db.execute('SELECT digest,path FROM chat_images WHERE actor=? AND origin=? ORDER BY at DESC LIMIT 1',(str(actor),origin)).fetchone()
        if row:
            path = (self.directory/row[1]).resolve()
            if path.parent==self.directory.resolve() and path.is_file():
                raw,suffix = await asyncio.to_thread(self.validate,path)
                if hashlib.sha256(raw).hexdigest()==row[0]:return raw,suffix
        raise ValueError('我这边没有可用的那张原图，请把图片再发一次。')

    async def save(self, actor, origin):
        raw,suffix = await self.source(actor,origin)
        digest = hashlib.sha256(raw).hexdigest()
        scope = hashlib.sha256((str(actor)+'\0'+origin).encode()).hexdigest()[:20]
        name = scope+'-'+digest+suffix
        path = self.directory/name
        with self.connect() as db:
            exists = db.execute('SELECT 1 FROM chat_images WHERE actor=? AND origin=? AND digest=?',(str(actor),origin,digest)).fetchone()
            if not exists:
                used = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
                if db.execute('SELECT COUNT(*) FROM chat_images').fetchone()[0]>=200 or used+len(raw)>64*1024*1024:
                    raise ValueError('图片收藏空间已满，这次没有保存。')
                temporary = path.with_suffix(path.suffix+'.tmp')
                temporary.write_bytes(raw);temporary.chmod(0o600);temporary.replace(path)
                db.execute('INSERT INTO chat_images VALUES (?,?,?,?,?)',(str(actor),origin,digest,name,self.clock()))
        self.pending.pop((str(actor),origin),None)
        return path

    def forget(self, actor, origin):
        with self.connect() as db:
            rows = db.execute('SELECT path FROM chat_images WHERE actor=? AND origin=?',(str(actor),origin)).fetchall()
            db.execute('DELETE FROM chat_images WHERE actor=? AND origin=?',(str(actor),origin))
        self.pending.pop((str(actor),origin),None)
        for (name,) in rows:
            path = (self.directory/name).resolve()
            if path.parent==self.directory.resolve():path.unlink(missing_ok=True)

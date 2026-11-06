"""Render lightweight Chinese greeting cards without paid model calls."""
from pathlib import Path
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
BOLD = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'


def render_card(row, directory, message=''):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    text = str(message).strip() or ('愿新的一岁有好多开心的小事，也有一直惦记你的人。鲸鲸把这份祝福送给你～' if row['kind'] == 'birthday' else '把值得记住的日子认真收藏，也把之后的日子一起慢慢过好。鲸鲸陪你记着～')
    if len(text) > 80 or any(ord(c) < 32 and c != '\n' for c in text):
        raise ValueError('祝福语请控制在 80 字以内。')
    image = Image.new('RGB', (1000,700))
    draw = ImageDraw.Draw(image)
    for y in range(700):
        t = y / 699
        draw.line((0,y,999,y), fill=tuple(round(a*(1-t)+b*t) for a,b in zip((241,251,255),(208,235,248))))
    draw.rounded_rectangle((35,35,965,665), radius=32, outline=(151,199,220), width=2)
    draw.text((70,65), '鲸鲸的小小祝福', font=ImageFont.truetype(FONT,26), fill=(57,105,132))
    draw.text((68,125), '生日快乐' if row['kind']=='birthday' else '纪念日快乐', font=ImageFont.truetype(BOLD,74), fill=(25,78,112))
    label = row['name'] if row['kind']=='birthday' else row['title']
    label_font = ImageFont.truetype(FONT,37)
    while draw.textlength('送给 '+label,font=label_font)>650:
        label=label[:-2]+'…'
    draw.text((73,245), '送给 '+label, font=label_font, fill=(40,89,119))
    # A simple whale drawn from native shapes remains editable in this source.
    draw.ellipse((785,218,912,302), fill=(87,165,204))
    draw.polygon(((792,258),(751,228),(754,286)), fill=(87,165,204))
    draw.ellipse((879,246,886,253), fill=(18,65,92))
    draw.arc((854,253,895,281), start=5,end=75, fill=(18,65,92), width=3)
    draw.arc((826,187,862,227), start=190,end=340, fill=(87,165,204), width=4)
    font = ImageFont.truetype(FONT,32)
    lines, current = [], ''
    for char in text:
        if char=='\n' or draw.textlength(current+char,font=font)>840:
            lines.append(current)
            current='' if char=='\n' else char
        else: current+=char
    if current: lines.append(current)
    if len(lines)>5: raise ValueError('祝福语换行太多，请缩短一点。')
    for index,line in enumerate(lines):
        draw.text((75,345+46*index), line, font=font, fill=(46,88,112))
    draw.text((75,610), '{:02d}月{:02d}日 · 哦鲸鲸'.format(row['month'],row['day']), font=ImageFont.truetype(FONT,24), fill=(69,117,143))
    for old in sorted(directory.glob('jingjing_card_*.png'),key=lambda p:p.stat().st_mtime,reverse=True)[19:]:
        old.unlink(missing_ok=True)
    path=directory/('jingjing_card_'+uuid.uuid4().hex+'.png')
    image.save(path,'PNG')
    path.chmod(0o600)
    return path

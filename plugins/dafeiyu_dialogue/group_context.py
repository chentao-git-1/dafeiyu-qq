"""Attributed group context from confirmed exchanges; no private or third-party cache history."""
import json


def history(items,limit=12,budget=8000):
    exchanges=sorted((r for r in items if r.kind=='exchange' and r.visibility=='group'),key=lambda r:r.recorded_at)[-limit:]
    pairs=[];used=0
    for row in reversed(exchanges):
        body=json.loads(row.text)
        pair=[{'role':'user','content':json.dumps({'speaker_qq':row.owner,'text':body['human']},ensure_ascii=False)},
            {'role':'assistant','content':body['reply']}]
        size=sum(len(p['content']) for p in pair)
        if used+size<=budget:pairs.append(pair);used+=size
    return [message for pair in reversed(pairs) for message in pair]


def quoted(event):
    from astrbot.core.message.components import Plain,Reply
    return [{'speaker_qq':str(p.sender_id),'message_id':str(p.id),
        'text':''.join(v.text for v in (p.chain or []) if isinstance(v,Plain))[:2000]}
        for p in event.get_messages() if isinstance(p,Reply)][:3]

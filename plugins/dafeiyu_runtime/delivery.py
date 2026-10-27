"""Shared, event-local record of QQ deliveries confirmed by the sender."""
import time

RECEIPTS_KEY = 'dafeiyu_confirmed_delivery'


def receipts(event):
    return event.get_extra(RECEIPTS_KEY, []) or []


def append_receipt(event, text, kind='reply'):
    confirmed = list(receipts(event))
    confirmed.append({'kind': kind, 'text': text, 'at': time.time()})
    event.set_extra(RECEIPTS_KEY, confirmed[-128:])


def reply_text(confirmed):
    return '\n'.join(dict.fromkeys(
        row['text'] for row in confirmed if row.get('kind') == 'reply' and row.get('text')
    ))

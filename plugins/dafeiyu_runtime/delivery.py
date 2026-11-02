"""Shared, event-local record of QQ deliveries confirmed by the sender."""
import time
import logging

RECEIPTS_KEY = 'dafeiyu_confirmed_delivery'


def receipts(event):
    return event.get_extra(RECEIPTS_KEY, []) or []


def append_receipt(event, text, kind='reply'):
    confirmed = list(receipts(event))
    confirmed.append({'kind': kind, 'text': text, 'at': time.time()})
    event.set_extra(RECEIPTS_KEY, confirmed[-128:])
    if kind == 'reply' and event.get_extra('dafeiyu_activity_input', None):
        try:
            from data.plugins.dafeiyu_social.access import record_delivery
            record_delivery(event)
        except ImportError:
            pass
        except Exception as error:
            # QQ delivery already succeeded. Metadata failure must not resend it.
            logging.getLogger(__name__).warning('Dialogue metadata not saved: %s', type(error).__name__)


def reply_text(confirmed):
    return '\n'.join(dict.fromkeys(
        row['text'] for row in confirmed if row.get('kind') == 'reply' and row.get('text')
    ))

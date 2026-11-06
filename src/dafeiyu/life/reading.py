"""Complete, bounded tool responses and model-selected public reading topics."""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit


class ReadingError(RuntimeError):
    def __init__(self, stage, code):
        if stage not in ('query', 'search', 'fetch') or not re.fullmatch(r'[a-z0-9_]{1,48}', code):
            raise ValueError('Invalid reading diagnostic')
        self.code = 'reading_' + stage + '_' + code
        super().__init__(self.code)


async def read_limited(stream, stage, limit=512 * 1024):
    # read(n) can return one early network chunk. Parse only after actual EOF.
    parts = []; size = 0
    while True:
        chunk = await stream.read(min(64 * 1024, limit - size + 1))
        if not chunk:
            return b''.join(parts)
        size += len(chunk)
        if size > limit:
            raise ReadingError(stage, 'response_too_large')
        parts.append(chunk)


def decode_response(raw, stage):
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReadingError(stage, 'invalid_json') from None
    if not isinstance(value, dict):
        raise ReadingError(stage, 'invalid_payload')
    return value


def checked_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Invalid public reading URL')
    try:
        parsed = urlsplit(value); host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise ValueError('Invalid public reading URL')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or host.endswith(('.localhost', '.local', '.internal')):
                raise ValueError('Invalid public reading host')
            ascii_host=host.encode('idna').decode('ascii')
            if (not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+',ascii_host)
                    or all(re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)',part) for part in ascii_host.split('.'))):
                raise ValueError('Invalid public reading host')
        else:
            if not address.is_global:
                raise ValueError('Invalid public reading host')
    except (TypeError, ValueError):
        raise ValueError('Invalid public reading URL') from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


async def explore(tool, question):
    """Browse result summaries once; selecting an interest shares the ordinary activity call."""
    if not isinstance(question, str) or not question.strip():
        raise ReadingError('query', 'missing')
    # The existing search service accepts at most 128 Unicode characters.
    query = question.strip()[:128]
    result = await tool('search', {'query': query, 'count': 8, 'need_content': False})
    rows = result.get('results')
    if not isinstance(rows, list):
        raise ReadingError('search', 'invalid_results')
    candidates = []; seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            url = checked_url(row.get('url'))
        except ValueError:
            continue
        if url in seen:continue
        seen.add(url)
        title=row.get('title') or url
        summary=row.get('snippet') or row.get('description') or row.get('summary') or ''
        if not isinstance(title,str) or not isinstance(summary,str):continue
        candidates.append(dict(id='search:'+hashlib.sha256(url.encode()).hexdigest()[:24],
            url=url,title=title[:200],summary=summary[:600]))
        if len(candidates)==8:break
    if not candidates:
        raise ReadingError('search', 'no_public_results')
    return dict(query=query,candidates=candidates,content_nature='search_summaries_not_full_articles')


def selected_discovery(source, generated):
    """A curiosity may be absent; an invented source cannot become a remembered discovery."""
    ident=generated.get('selected_source_id');evidence=generated.get('evidence','')
    if ident is None or ident=='':
        if evidence:raise ValueError('Unselected discovery has source evidence')
        return dict(query=source['query'],content_nature=source['content_nature'],selected=None)
    selected=next((r for r in source['candidates'] if r['id']==ident),None)
    if (selected is None or not isinstance(evidence,str) or not evidence.strip()
            or len(evidence)>200 or evidence not in selected['title']+'\n'+selected['summary']):
        raise ValueError('Discovery lacks offered source evidence')
    return dict(query=source['query'],content_nature=source['content_nature'],selected=selected)


async def read_article(tool, question, selected_url=None):
    if selected_url:
        url=checked_url(selected_url);row={};query=question.strip()[:128]
    else:
        overview=await explore(tool,question)
        row=overview['candidates'][0];url=row['url'];query=overview['query']
    extracted = await tool('fetch', {'url': url})
    body = extracted.get('content')
    if not isinstance(body, str) or not body.strip():
        raise ReadingError('fetch', 'empty_content')
    body = body.strip()[:16000]
    title = extracted.get('title') or row.get('title') or url
    if not isinstance(title, str):
        raise ReadingError('fetch', 'invalid_title')
    return dict(title=title[:200], body=body, url=url, query=query,
        text_sha256=hashlib.sha256(body.encode()).hexdigest(), transport='existing_deepseek_official_fetch')


def capabilities():
    return dict(source_scope='public_https_web', query_source='model_selected_question',
        max_query_characters=128, may_revisit_sources=True, max_search_requests=1, max_fetch_requests=1,
        exploring=dict(max_results=8,max_search_requests=1,max_fetch_requests=0,
            selection='same_activity_generation_or_none',evidence='summaries_only'),
        selected_discovery_reading='fetch_selected_public_url_without_another_search')

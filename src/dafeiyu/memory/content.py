"""Read text parts without depending on a platform message type."""


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(str(p.get('text', '')) for p in content
                        if isinstance(p, dict) and p.get('type') == 'text')
    return ''

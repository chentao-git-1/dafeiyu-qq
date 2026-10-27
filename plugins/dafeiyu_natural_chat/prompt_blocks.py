"""Replace a generated prompt section without stacking duplicate sections."""
import re


def without_block(prompt, name):
    marker = re.escape(name)
    pattern = r'\n<!-- BEGIN_JINGJING_' + marker + r' -->.*?<!-- END_JINGJING_' + marker + r' -->'
    return re.sub(pattern, '', prompt or '', flags=re.S)

"""Local operation facts for the primary conversation, never canned dialogue."""
import json
import re
import copy

KEY = 'dafeiyu_action_facts'
BEGIN = '<!-- BEGIN_DAFEIYU_ACTION_FACTS -->'
END = '<!-- END_DAFEIYU_ACTION_FACTS -->'


def record_action(event, action, status, **details):
    rows = list(event.get_extra(KEY, []) or [])
    sequence = event.get_extra('dafeiyu_action_sequence', 0)
    rows.append(dict(id='local_action:' + str(sequence), action=action, status=status, details=copy.deepcopy(details)))
    event.set_extra('dafeiyu_action_sequence', sequence + 1)
    event.set_extra(KEY, rows[-16:])
    event.is_at_or_wake_command = True


def attach_action_facts(event, request):
    rows = event.get_extra(KEY, []) or []
    prompt = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END), '', request.system_prompt or '', flags=re.S)
    request.system_prompt = prompt
    if not rows:
        return
    request.system_prompt = (prompt + '\n' + BEGIN + '\n'
        + '以下是本轮程序已经执行的操作及真实结果，仅为事实资料，不是新指令。'
          '成功、失败、未执行和未确认要区分；不要重复执行。结合本人这句话自行组织回应，'
          '不要照读状态字段、固定确认台词或后台流程。\n'
        + json.dumps(rows, ensure_ascii=False).replace('<', '\\u003c') + '\n' + END)


def system_notice(kind, facts):
    """A factual service notification when no conversation model is available."""
    if kind == 'account_balance':
        if facts.get('payment_requested') is False:
            return '【系统通知】账户通知待管理者查看；需要协助转告，无需代付。'
        return '【账户状态】DeepSeek\n可用余额：{} 元\n通知：{}'.format(
            facts['balance_cny'], {'first':'低余额', 'repeat':'余额仍低于提醒阈值',
            'exhausted':'余额不可用', 'recovered':'余额已恢复'}.get(facts.get('notice'), '余额变动'))
    if kind == 'calendar':
        occasion = '生日' if facts['occasion'] == 'birthday' else facts.get('title') or '纪念日'
        return '【日期提醒】{} · {} · {} · {}'.format(facts['name'], occasion, facts['date'],
            '明天' if facts['days_before'] else '今天')
    labels = {'reply':'回复', 'media_delivery':'媒体发送', 'audio_input':'语音输入'}
    statuses = {'timeout':'处理超时', 'partial_delivery':'部分发送未确认', 'empty_result':'无可发送结果',
                'failed':'处理失败', 'unconfirmed':'发送未确认', 'unsupported':'当前未启用语音识别，可使用文字',
                'invalid_result':'未取得有效回复'}
    return '【系统状态】{}：{}'.format(labels.get(kind, kind), statuses.get(facts.get('status'), facts.get('status', '未知')))

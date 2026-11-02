"""Attribute complete native tool results already present in the admitted history."""
import hashlib
import json


def historical_tools(history,budget=7000,limit=4):
    calls={};rows=[];seen=set()
    for message in list(history)[-14:]:
        if not isinstance(message,dict):continue
        if message.get('role')=='assistant':
            for call in message.get('tool_calls',[]) or []:
                if not isinstance(call,dict):continue
                function=call.get('function',{})
                if isinstance(function,dict) and isinstance(function.get('name'),str) and call.get('id'):
                    key=str(call['id'])
                    if key in calls: calls[key]=None
                    else:calls[key]=function['name']
        elif message.get('role')=='tool':
            key=str(message.get('tool_call_id',''));name=calls.get(key);content=message.get('content')
            if isinstance(content,list) and content and all(isinstance(p,dict) and p.get('type')=='text' and isinstance(p.get('text'),str) for p in content):
                content=''.join(p['text'] for p in content)
            if not name or key in seen or not isinstance(content,str) or not content.strip() or len(content)>5000:continue
            seen.add(key)
            signature=hashlib.sha256(json.dumps([key,name,content],ensure_ascii=False).encode()).hexdigest()[:24]
            rows.append((key,dict(id='history_tool:'+signature,tool=name,result=content,current_turn=False)))
    picked=[];used=0
    for key,row in reversed(rows):
        if not calls.get(key):continue
        size=len(json.dumps(row,ensure_ascii=False))
        if len(picked)<limit and used+size<=budget:picked.append(row);used+=size
    return list(reversed(picked))

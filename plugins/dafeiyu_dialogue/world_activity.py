"""Turn-scoped FishWorld facts and deliberate QQ sharing on autonomous wakes."""
import hashlib
import json
import re
import time
from dataclasses import replace

from dafeiyu.dialogue.generation import _generation_object, parse_generation
from .world_observation import binding_scope


FLOW = "dafeiyu_world_activity"
EXT = "dafeiyu_world_choice"
FALLBACK = {"quiet", "progress", "chat"}
WEAR = re.compile(r"^·\s*(.{1,45}?)\s+([\w-]{1,40})\s+——\s*(.+)$")


def fish_for(plugin, event, scope=None):
    meta=plugin.context.get_registered_star("astrbot_plugin_fishworld")
    fish=getattr(meta,"star_cls",None) if getattr(meta,"activated",False) else None
    if not fish or not hasattr(fish,"follow_scope"):
        return None
    actual=fish.follow_scope(event)
    return fish if actual and (scope is None or actual==scope) else None


def raw_text(result):
    if isinstance(result,str):
        return result
    parts=result.get("content") if isinstance(result,dict) else getattr(result,"content",None)
    vals=[]
    for p in parts if isinstance(parts,list) else ():
        kind=p.get("type") if isinstance(p,dict) else getattr(p,"type",None)
        val=p.get("text") if isinstance(p,dict) else getattr(p,"text",None)
        if kind=="text" and isinstance(val,str):
            vals.append(val)
    return "\n".join(vals)


def now_facts(text):
    fields, wardrobe = {}, {}
    in_closet=False
    for line in str(text or "").splitlines():
        line=line.strip()
        if line.startswith("衣柜（") and "world_update" in line:
            in_closet=True
            continue
        if in_closet:
            match=WEAR.fullmatch(line)
            if match:
                wardrobe[match[2]]=match[1]
                if "正穿着" in match[3]:
                    fields["worn_id"]=match[2]
                continue
            in_closet=False
        label,sep,value=line.partition("：")
        if sep and label in ("你在","在做","同时","穿着","自行车","今天到现在") and label not in fields:
            fields[label]=value[:900]
    fields["wardrobe"]=wardrobe
    summary=fields.pop("今天到现在","")
    fields["bought"]=next((p.strip()[2:].strip() for p in summary.split("；") if p.strip().startswith("买过")),"")
    return fields


def parse_scoped(event, text):
    flow=event.get_extra(FLOW)
    if not flow:
        return parse_generation(text,recover_claims=True,recover_metadata=True)
    obj, recovered=_generation_object(text,True)
    extra=obj.pop("fishworld",{})
    if not isinstance(extra,dict):
        extra={}
    share=extra.get("share")
    choice={"share":share if isinstance(share,str) and share in FALLBACK else "quiet",
            "topic":str(extra.get("topic") or "")[:75],
            "photo":extra.get("photo") if isinstance(extra.get("photo"),dict) else {}}
    # The ordinary generation validator expects a spoken reply. This internal text can
    # only stand in for an intentionally quiet, verified synthetic wake.
    if flow["auto"] and choice["share"]=="quiet" and obj.get("reply")=="":
        obj["reply"]="本轮自主活动。"
    generated=parse_generation(json.dumps(obj,ensure_ascii=False),recover_claims=True,recover_metadata=True)
    event.set_extra(EXT,choice)
    return replace(generated,framing_recovered=True) if recovered else generated


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def tokens(text):
    chunks=re.findall(r"[\u3400-\u9fff]{2,}|[a-z]{2,}",text.lower())
    return {chunk[n:n+2] for chunk in chunks for n in range(len(chunk)-1)}


def sharing_state(facts):
    place=facts.get("你在","")
    trip=re.search(r"去(.{1,40}?)的路上",place)
    if trip:
        eta=re.search(r"\d{1,2}:\d{2}\s*到",place)
        place=trip[1]+(eta[0] if eta else "")
    def activity(s):
        return re.sub(r"还剩\s*\d+\s*分钟|已(?:经)?\s*\d+\s*分钟|剩余\s*\d+\s*分钟","",s)
    return digest("|".join([place,activity(facts.get("在做","")),activity(facts.get("同时","")),
                            facts.get("穿着",""),facts.get("自行车","")]))


def proposed_share(event, body, choice=None):
    f=event.get_extra(FLOW) or {}
    choice=choice or event.get_extra(EXT) or {}
    kind=choice.get("share","quiet")
    if not f.get("auto") or not body.strip() or kind not in ("progress","chat"):
        return None
    text=body.strip()
    norm=re.sub(r"[\W\d_]+","",text)
    if not norm:
        return None
    topic=re.sub(r"[\W\d_]+","",str(choice.get("topic") or ""))
    state=sharing_state(f.get("facts") or {})
    changed=f.get("change") or ""
    signals=f.get("signals",[])
    outcome=digest(changed+"|"+json.dumps(signals,sort_keys=True,ensure_ascii=False)) if changed or signals else ""
    words=sorted(digest(w)[:12] for w in tokens(norm))[:70]
    row=dict(state=state,event=outcome,topic=digest(topic or norm),words=words,stamp=time.time())
    for old in reversed(f.get("shares",[])):
        if time.time()-old["stamp"] > 86400:
            continue
        overlap=len(set(words)&set(old["words"]))/max(1,min(len(words),len(old["words"])))
        new_event=bool(outcome and outcome!=old["event"])
        if (old["topic"]==row["topic"] and ((old["state"]==state and not new_event) or overlap>=0.65)) or overlap>=0.80:
            return None
        if old["state"]==state and not new_event and (kind=="progress" or overlap>=0.48):
            return None
    return row


def quiet_result(event,response):
    chain=getattr(response,"result_chain",None)
    if chain is not None and isinstance(getattr(chain,"chain",None),list):
        chain.chain.clear()
    response.result_chain=None
    response.completion_text=""
    event.set_extra("dafeiyu_generation_final_text","")
    event.set_extra("dafeiyu_generation_delivery_only",True)
    event.set_extra("dafeiyu_world_quiet",True)


def add_packet(request,name,data):
    request.system_prompt+=("\n<!-- BEGIN_DAFEIYU_"+name+" -->\n"
                           +json.dumps(data,ensure_ascii=False).replace("<","\\u003c")
                           +"\n<!-- END_DAFEIYU_"+name+" -->")

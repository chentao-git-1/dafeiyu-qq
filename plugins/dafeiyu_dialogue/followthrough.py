"""Connect world observations, accepted photo requests, and optional owner updates."""
import re

from .route_receipts import current_route, fish_tool
from .world_activity import FLOW, add_packet, fish_for, now_facts, proposed_share, raw_text
from .wardrobe import (CANCEL, CLOTHES, OTHER_IMAGE, PHOTO_DONE, REPAIRED, SPOKEN_WEAR,
                       outfit_wording, photo_followup, photo_intent, prepare_photo,
                       settle_choice, swap, update_receipt)
from .world_outing import get_budget, read_world, recent_errand, subject, timed_wording


def auto_wake(event):
    return (bool(event.get_extra("fishworld_wake_id")) and
            str(getattr(getattr(event,"message_obj",None),"message_id","")).startswith("fishworld_")
            and not event.get_extra("fishworld_manual_wake") and not event.get_extra("fishworld_manual_test"))


async def begin(plugin,event,req,source,items,turn):
    if event.get_extra("fishworld_manual_test") or event.get_extra("cron_job") or event.get_extra("cron_payload"):
        return
    fish=fish_for(plugin,event)
    snap=fish.follow_snapshot(event) if fish else None
    if not snap:
        return
    scope,pending,shares=snap
    automatic=auto_wake(event)
    human="" if event.get_extra("fishworld_wake_id") else str(source.get("text") or "")
    intent=photo_intent(human)
    resume=bool(pending and ((automatic and pending["stage"] in ("waiting","changing"))
                or photo_followup(human)))
    f=dict(context=plugin.context, event=event, scope=scope, auto=automatic, human=human,
           intent=intent,resume=resume,pending=pending,offer=None,shares=shares,tools=req.func_tool,
           facts={},view=None,signals=event.get_extra("fishworld_completion_signals",[]) or [])
    event.set_extra(FLOW,f)
    if pending and not OTHER_IMAGE.search(human) and CANCEL.search(human):
        await swap(plugin,event,f,dict(pending,stage="cancelled"))
        f.update(intent=None,resume=False,cancelled=True)
    f["outing"]=subject(human) or (recent_errand(items,turn) if automatic else None)
    if (f.get("intent") or f.get("resume")) and not event.get_extra("fishworld_prefetch_world_now") and fish_tool(req.func_tool,"world_now"):
        event.set_extra("dafeiyu_current_world_detail",True)
        event.set_extra("dafeiyu_detail_is_bicycle",False)


async def attach(plugin,event,req):
    f=event.get_extra(FLOW)
    if not f or not fish_for(plugin,event,f["scope"]):
        return
    if not f["facts"] and event.get_extra("dafeiyu_prefetched_world_now"):
        f["facts"]=now_facts(event.get_extra("dafeiyu_prefetched_world_now"))
    if f["intent"] or f["resume"] or f["outing"]:
        f["view"]=await current_route(plugin.context,event,f["scope"],details=True)
    packet={}
    if f["intent"] or f["resume"]:
        packet["约定的穿搭照片"]=await prepare_photo(plugin,event,req,f)
        packet["今天买过"]=f["facts"].get("bought","")
    if f["outing"]:
        packet["限时外出"]=await get_budget(plugin,event,f,f["outing"])
    if f["auto"]:
        packet["自主活动"]=True
        packet["本轮新事件"]=[s["kind"] for s in f["signals"]]
    if f.get("cancelled"):packet["此前的照片约定已取消"]=True
    if packet:
        add_packet(req,"WORLD_FOLLOWUP",packet)


async def tool_result(plugin,event,tool,args,result):
    f=event.get_extra(FLOW)
    if not f or getattr(tool,"mcp_server_name",None)!="fish-world" or not fish_for(plugin,event,f["scope"]):
        return
    err=result.get("isError",False) if isinstance(result,dict) else getattr(result,"isError",False)
    if tool.name=="world_update" and args.get("outfit"):
        await update_receipt(plugin,event,f,args,result)
        return
    if err:return
    text=raw_text(result)
    if tool.name=="world_now" and text:
        f["facts"]=now_facts(text)
    elif tool.name=="world_do" and args.get("id") and text:
        if not str(args["id"]).startswith(("look-","try-")) and not re.search(r"失败|不能|没有这个|无法|做不了",text[:120]):
            f["change"]=str(args["id"])
        if re.search(r"(?:后胎|车胎|轮胎).{0,12}(?:补好|修好|补上|修复)了",text) and not re.search(r"还没|如果|需要|要是",text):
            f["repair_done"]=True
    elif tool.name=="world_go" and args.get("by") and text and not re.search(r"不能|失败|无法",text[:120]):
        f["change"]="出发 "+str(args.get("place",""))


async def wording(plugin,event,text):
    f=event.get_extra(FLOW)
    if not f or not fish_for(plugin,event,f["scope"]) or not text:
        return text
    if f.get("offer"):
        await settle_choice(plugin,event,f,text)
    need_wear=bool((CLOTHES.search(text) and (SPOKEN_WEAR.search(text) or "没买" in text)) or
                   (f.get("intent") or f.get("resume")) and PHOTO_DONE.search(text))
    if (need_wear or REPAIRED.search(text)) and not f.get("facts") and not f.get("looked"):
        f["looked"]=True
        f["facts"]=now_facts(await read_world(f,"world_now",{}))
    if need_wear and not f.get("view"):
        f["view"]=await current_route(plugin.context,event,f["scope"],details=True)
    return await timed_wording(plugin,event,f,outfit_wording(text,f,event))


async def delivered(plugin,event):
    row=event.get_extra("dafeiyu_share_offer")
    f=event.get_extra(FLOW)
    if not row or not f or not f["auto"] or event.get_extra("dafeiyu_share_saved"):
        return
    receipts=event.get_extra("dafeiyu_confirmed_delivery",[]) or []
    if not any(r.get("kind")=="reply" and (r.get("text") or r.get("media_types")) for r in receipts):
        return
    fish=fish_for(plugin,event,f["scope"])
    if fish:
        await fish.remember_share(event,f["scope"],row)
        event.set_extra("dafeiyu_share_saved",True)


def final_share(event,body):
    f=event.get_extra(FLOW)
    if not f or not f["auto"]:
        return True
    if f.get("image_done") or event.get_extra("dafeiyu_share_tool_sent"):
        return False
    row=proposed_share(event,body)
    if not row:
        return False
    event.set_extra("dafeiyu_share_offer",row)
    return True

"""A requested portrait follows the outfit the world confirms and a real QQ send."""
import copy
from datetime import datetime, timedelta
import hashlib
import re
import time

from .route_receipts import current_route, fish_tool, latest_role
from .world_activity import EXT, FLOW, fish_for
from .world_observation import SHANGHAI


CLOTHES=re.compile(r"衣服|新衣|衣裳|穿搭|那[件套]|毛衣|卫衣|棉服|外套|穿的|换一身")
CHANGE=re.compile(r"换|穿上|试穿")
SHOW=re.compile(r"看看|看你|给我看|拍|照片|自拍|发.{0,3}图")
CANCEL=re.compile(r"(?:别|不用|不要|取消|先不|不想看).{0,6}(?:拍|照片|发图|给我看)|(?:照片|自拍).{0,6}(?:算了|不用了|别发|取消)")
AFTER_MEAL=re.compile(r"(?:吃完|吃好了|饭后|吃过饭|吃了饭|扒(?:完|几口)).{0,24}(?:再|就|换|拍|给你看)")
LATER=re.compile(r"(?:等会|一会儿|晚点|稍后|待会|过会|明天|以后).{0,20}(?:换|拍|发.{0,4}图)|(?:换好|换完).{0,12}(?:拍|给你看)")
SPOKEN_WEAR=re.compile(r"(?:我|身上|里面|里头|今天|裤子).{0,12}(?:穿着|穿的是|是|没换)|"
                       r"已经.{0,5}(?:换|穿)|换(?:好|上)(?:了|啦)|穿上(?:了|试)|(?:穿|换)成了|试穿了")
COLOR=re.compile(r"(黑|白|灰|红|蓝|绿|黄|紫|粉|棕|橙|米)(?:色)?(?:加绒|厚|薄|休闲|宽松|工装|牛仔|棉布|长|短){0,3}(裤|裙)")
REPAIRED=re.compile(r"(?:我(?:今天|刚才|刚|已经|只|亲自)*|后胎|车胎|轮胎|胎)(?:把.{0,5})?(?:补了|修了|补好|修好)|(?:修好|补好)了(?:后胎|车胎|轮胎)")
PAST_PURCHASE=re.compile(r"(?:没(?:有)?(?:买|购入)|什么也没买|啥也没买)")
PHOTO_DONE=re.compile(r"拍好(?:了|啦)|拍完(?:了|啦)|照片.{0,4}发(?:给你|出去|过去)了|(?:已经|刚刚?)(?:拍了|发了图)|图发了")
OTHER_IMAGE=re.compile(r"海报|配图|帮.{0,6}画|画(?:一张|个|幅|一下)|漫画|设计图|参考图|截图|风景照|(?:[他她]|别人)的(?:照片|自拍)|看看(?:聊天|记录|地图|行程|消息)")
RETAKE=re.compile(r"重新拍|再拍一(?:次|张)|重拍|补拍")


def photo_followup(text):
    if not text or OTHER_IMAGE.search(text):
        return False
    return bool(RETAKE.search(text) or re.search(
        r"(?:那张|刚才的|答应.{0,5}的)(?:穿搭照|照片|自拍|图)|"
        r"(?:照片|自拍)(?:呢|拍好|发了|什么时候)|"
        r"(?:什么时[候间]|现在就|等会再|饭后再|吃完再)(?:换|拍)|"
        r"(?:换|拍)(?:好了?[没吗呢]|完了?[没吗呢])|(?:拍|发)给我了?[没吗]",text))


def photo_intent(text):
    if not isinstance(text,str) or len(text)>350 or CANCEL.search(text) or OTHER_IMAGE.search(text) or re.search(r"假如|举例|编个|帮我写",text):
        return None
    if not SHOW.search(text):
        return None
    changing=bool(CLOTHES.search(text) and CHANGE.search(text))
    if changing or re.search(r"看看你|看你.{0,10}(?:样子|穿搭|穿着)|你的(?:照片|自拍)|你.{0,12}(?:自拍|拍一张)|(?:自拍|拍个你)",text):
        return "change" if changing else "current"
    return None


def portrait_prompt(prompt,worn):
    # The model still chooses composition and setting. The current outfit is the
    # sole clothing description, including when an old drawing said otherwise.
    pieces=re.split(r"[，,。；;\n]+",str(prompt or ""))
    scene="，".join(p.strip() for p in pieces if p.strip() and not re.search(
        r"毛衣|卫衣|棉服|睡衣|外套|衬[衣衫]|(?:长|短|牛仔|白|黑|灰)裤|裙子|拉链|身穿|穿着|上衣|下装",p))
    head="陈听澜这张照片里的完整穿着是"+worn["look"]+"。"
    return (head+scene)[:1500]


def option_ids(facts,human,changing,view):
    current=(view or {}).get("outfit",{}).get("id") or facts.get("worn_id","")
    closet=facts.get("wardrobe",{})
    if not changing:
        return [current] if current else []
    mentioned=[ident for ident,label in closet.items() if label in human]
    if not mentioned:
        # A distinctive part of a garment's name can identify it ("砖红那件").
        fragments={ident:{label[i:i+2] for i in range(len(label)-1)} for ident,label in closet.items()}
        matched=[ident for ident,words in fragments.items() if any(w in human for w in words)]
        if len(matched)==1:
            mentioned=matched
    if mentioned:
        return mentioned
    choices=[i for i,label in closet.items() if label in facts.get("bought","")] if re.search(r"新衣|新买|今天.{0,5}(?:新|买)",human) else list(closet)
    return [i for i in choices if i!=current] or choices


def new_offer(event,f,view):
    human=f["human"];facts=f["facts"]
    change=f["intent"]=="change"
    choices=option_ids(facts,human,change,view)
    mid=str(getattr(getattr(event,"message_obj",None),"message_id","") or time.time_ns())
    ident=hashlib.sha256(("\n".join(f["scope"])+mid).encode()).hexdigest()[:24]
    now=time.time();local=datetime.fromtimestamp(now,SHANGHAI)
    tomorrow=(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    start=tomorrow if "明天" in human else now
    return {"id":ident,"created":now,"not_before":start,
            "expires":tomorrow if re.search(r"今天|今晚",human) and start==now else now+7*86400,
            "target":choices[0] if len(choices)==1 else "", "choices":choices,
            "initial":(view or {}).get("outfit",{}).get("id") or facts.get("worn_id",""),
            "change":change,"stage":"waiting","wait":"meal" if AFTER_MEAL.search(human) else "ready",
            "meal":"","meal_until":0,"change_at":0}


async def swap(plugin,event,f,new):
    fish=fish_for(plugin,event,f["scope"])
    previous=f.get("pending")
    if not fish or not await fish.change_photo(event,f["scope"],previous,new):
        return False
    f["pending"]=copy.deepcopy(new)
    f["offer"]=copy.deepcopy(new)
    return True


def fresh_outfit(view):
    outfit=(view or {}).get("outfit") or {}
    return outfit if outfit.get("id") and outfit.get("look") else {}


def due_photo(row,view):
    if not row or row["stage"] in ("held","sending","sent","cancelled") or not row["not_before"]<=time.time()<row["expires"]:
        return ""
    if row["wait"]=="meal":
        return ""
    worn=fresh_outfit(view)
    if not worn:
        return ""
    if row["target"]==worn["id"] or not row["change"] and worn["id"] in row["choices"]:
        return "draw"
    if row["stage"]=="changing":
        return ""
    return "change" if row["choices"] else ""


async def refresh_meal(plugin,event,f,view):
    row=f.get("pending")
    if not row or row["wait"]!="meal" or row["stage"] not in ("waiting","changing"):
        return
    tasks=(view or {}).get("activities") or []
    meal=next((a for a in tasks if a["name"].startswith(("吃","用餐","喝粥"))),None)
    upd=dict(row)
    if meal:
        try:
            upd.update(meal=meal["name"][:100],meal_until=datetime.fromisoformat(meal["until"]).timestamp())
        except (ValueError,TypeError):
            return
    else:
        for sig in f["signals"]:
            label=re.split("[：:]",sig["text"])[0].removesuffix("完成了").removesuffix("结束了").strip()
            if (sig["kind"]=="done" and type(sig.get("t")) is int and sig["t"]/1000>=row["created"]-1
                    and label.startswith(("吃","用餐","喝粥")) and (not row["meal"] or label==row["meal"])):
                upd["wait"]="ready"
                break
        # If the notification was delayed/lost, check the fish's own timeline
        # before treating a finished meal as the trigger. Cooking alone never suffices.
        if (upd["wait"]=="meal" and view and (not tasks or row["meal"]) and
                (not row["meal_until"] or row["meal_until"]<=time.time())):
            from .world_outing import read_world
            day=datetime.fromtimestamp(row["meal_until"] or time.time(),SHANGHAI).date().isoformat()
            raw=await read_world(f,"world_history",{"date":day,"query":row["meal"] or "结束了","limit":12})
            for line in raw.splitlines():
                m=re.fullmatch(r"(\d\d:\d\d)\s+(.+)",line.strip())
                if not m:
                    continue
                end=re.fullmatch(r"(?:(?:完成|结束)：(.+)|(.+?)(?:结束了|完成了))",m[2])
                label=(end[1] or end[2]) if end else ""
                if not label.startswith(("吃","用餐","喝粥")) or (row["meal"] and label!=row["meal"]):
                    continue
                stamp=datetime.fromisoformat(day+"T"+m[1]+":00+08:00").timestamp()
                if row["meal"] and row["meal_until"]:
                    # History shows minutes. The precise end of this meal was
                    # observed while she was still eating it.
                    earliest=int(max(row["created"],row["meal_until"])//60)*60
                else:
                    earliest=row["created"]
                if stamp >= earliest:
                    upd["wait"]="ready"
                    break
    if upd!=row:
        await swap(plugin,event,f,upd)


async def prepare_photo(plugin,event,req,f):
    row=f.get("pending")
    if row and row["expires"]<=time.time():
        await swap(plugin,event,f,None)
    if not f.get("intent") and not f.get("resume"):
        return None
    view=f["view"]
    if f.get("intent"):
        f["offer"]=new_offer(event,f,view)
        previous=f.get("pending")
        if previous:
            same_outfit=(previous["change"]==f["offer"]["change"] and
                         (previous["choices"]==f["offer"]["choices"] or previous["target"] in f["offer"]["choices"]))
            if (previous["id"]==f["offer"]["id"] or previous["stage"]=="sending" or
                    previous["stage"]=="changing" and same_outfit):
                f["offer"]=copy.deepcopy(previous)
    elif row and row["stage"] in ("held","sent","cancelled") and RETAKE.search(f["human"]):
        f["offer"]=new_offer(event,f,view)
        if row["target"]:
            f["offer"].update(target=row["target"],choices=[row["target"]],change=row["change"])
    else:
        await refresh_meal(plugin,event,f,view)
        f["offer"]=copy.deepcopy(f.get("pending"))
    row=f.get("offer")
    if not row:
        return None
    if (row["stage"]=="changing" and time.time()-row["change_at"]>210 and fresh_outfit(view)
            and fresh_outfit(view)["id"]!=row["target"] and not any("换" in a["name"] for a in view["activities"])):
        await swap(plugin,event,f,dict(row,stage="held"))
        row=f["offer"]
    if not row["choices"] and f.get("facts",{}).get("wardrobe"):
        choices=option_ids(f["facts"],"新衣" if row["change"] else "",row["change"],view)
        upd=dict(row,choices=choices,target=choices[0] if len(choices)==1 else "")
        if choices:
            if row==f.get("pending"):await swap(plugin,event,f,upd)
            else:f["offer"]=upd
        row=f["offer"]
    stage=due_photo(row,view)
    req._dafeiyu_outfit_choice=(stage,tuple(row["choices"])) if stage else None
    return {"步骤":stage or row["stage"],"等待":row["wait"],"本次要换装":row["change"],"目标":row["target"],
            "可选":{i:f["facts"].get("wardrobe",{}).get(i,i) for i in row["choices"]},
            "当前穿着":fresh_outfit(view),"正在做":(view or {}).get("activities",[]),
            "已记为后续事项":bool(f.get("pending") and row["id"]==f["pending"]["id"])}


def outfit_tools(kwargs):
    ctx=kwargs.get("contexts")
    opt=getattr(ctx,"outfit_choice",None)
    if not opt or latest_role(ctx)!="user" or kwargs.get("tool_calls_result"):
        return kwargs
    stage,choices=opt;original=kwargs.get("func_tool")
    see=fish_tool(original,"world_now")
    action=fish_tool(original,"world_update") if stage=="change" else original.get_tool("dafeiyu_draw") if original else None
    if not (see and action):
        return kwargs
    if stage=="change":
        action=copy.copy(action)
        action.parameters={"type":"object","properties":{"outfit":{"type":"string","enum":list(choices)}},
                           "required":["outfit"],"additionalProperties":False}
    tools=copy.copy(original);tools.tools=[action,see]
    return dict(kwargs,func_tool=tools,tool_choice="required")


async def update_receipt(plugin,event,f,args,result):
    row=f.get("offer")
    target=args.get("outfit")
    if not row or target not in row["choices"] or row["stage"] in ("sending","held","sent","cancelled"):
        return
    from .world_activity import raw_text
    report=raw_text(result)
    errored=result.get("isError",False) if isinstance(result,dict) else getattr(result,"isError",False)
    if errored or not report or re.search(r"不能|无法|没有这|不在|换不了|找不到|失败|没这",report):
        f["outfit_failed"]=True
        if row==f.get("pending"):
            await swap(plugin,event,f,dict(row,stage="held"))
        return
    upd=dict(row,target=target,stage="changing",wait="ready",change_at=time.time())
    await swap(plugin,event,f,upd)
    if re.search(r"开始|正在换|分钟后|换好时间",report):
        f["change_ack"]=target
    f["view"]=await current_route(plugin.context,event,f["scope"],details=True)


async def reserve_change(plugin,event,f,args):
    row=f.get("offer")
    if not row or not args.get("outfit"):
        return
    target=args["outfit"]
    if (row["stage"]!="waiting" or row["wait"]=="meal" or target not in row["choices"]
            or time.time()<row["not_before"] or time.time()>=row["expires"]):
        args.pop("outfit",None)
        return
    attempted=dict(row,target=target,stage="changing",change_at=time.time())
    if not await swap(plugin,event,f,attempted):
        args.pop("outfit",None)


async def settle_choice(plugin,event,f,text):
    row=f.get("offer")
    if not row or row["stage"] in ("sending","held","sent","cancelled") or f.get("image_done"):
        return
    raw=(event.get_extra(EXT) or {}).get("photo") or {}
    op=raw.get("op")
    if op=="cancel" and not f.get("auto"):
        await swap(plugin,event,f,dict(row,stage="cancelled"))
        return
    if op=="cancel" and f.get("pending"):
        await swap(plugin,event,f,dict(row,stage="cancelled"))
        return
    after=raw.get("after") if op=="defer" else None
    if after not in ("meal","later","ready"):
        after="meal" if AFTER_MEAL.search(text) else "later" if LATER.search(text) and not f.get("pending") else None
    if not after:
        return
    target=raw.get("outfit")
    target=target if target in row["choices"] else row["target"]
    if not target and row["choices"]:
        closet=f["facts"].get("wardrobe",{})
        named=[i for i in row["choices"] if closet.get(i) and closet[i] in text]
        if len(named)==1:target=named[0]
    upd=dict(row,wait=after,target=target)
    if after=="later" and not f.get("pending"):
        upd["not_before"]=min(row["expires"],max(row["not_before"],time.time()+1))
    if upd!=f.get("pending"):
        if not await swap(plugin,event,f,upd):
            f["save_failed"]=True
    if after=="meal":
        await refresh_meal(plugin,event,f,f.get("view"))


async def guard_draw(plugin,event,*,sending=False):
    f=event.get_extra(FLOW)
    if f and f.get("cancelled"):
        return None,"这次照片已取消。"
    if not f or not (f.get("intent") or f.get("resume")):
        return None, ""
    row=f.get("offer")
    if not row:
        return None,"这次照片的约定已失效。"
    fish=fish_for(plugin,event,f["scope"])
    snap=fish.follow_snapshot(event) if fish else None
    if not snap or snap[1]!=f.get("pending"):
        return None,"这次照片的约定已经更新，旧请求没有发送。"
    view=await current_route(plugin.context,event,f["scope"],details=True)
    f["view"]=view
    worn=fresh_outfit(view)
    if sending:
        latest=fish.follow_snapshot(event)
        if (not latest or latest[1]!=row or row["stage"]!="sending" or not worn
                or worn["id"]!=row["target"] or time.time()>=row["expires"]):
            return None,"发送前未能确认原约定的穿着，本图没有发送。"
        return worn,""
    if due_photo(row,view)!="draw":
        return None,"本轮还没核实穿上约定的衣服，请按实际换装或等待进度回答。"
    reserved=dict(row,stage="sending",target=worn["id"])
    if not await swap(plugin,event,f,reserved):
        return None,"同一张照片已在处理或约定已更新。"
    return worn,""


async def finish_draw(plugin,event,success):
    f=event.get_extra(FLOW)
    if not f or not f.get("offer") or f["offer"]["stage"]!="sending":
        return
    row=f["offer"]
    if success:f["image_done"]=True
    await swap(plugin,event,f,dict(row,stage="sent" if success else "held"))


def outfit_wording(text,f,event):
    facts=f.get("facts") or {}
    worn=fresh_outfit(f.get("view")) or {"id":facts.get("worn_id",""),"look":facts.get("穿着","")}
    closet=facts.get("wardrobe",{})
    parts=re.findall(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+",text)
    fixed=[]
    bought=[re.sub(r"^\s*\d+\s*[套件条双]\s*","",name).strip()
            for name in re.split("[、，]",facts.get("bought","")) if re.search(r"衣|毛衫|裙|裤|泳装",name)]
    actual_colors={m[2]:m[1] for m in COLOR.finditer(worn["look"])}
    offer=f.get("offer") or {}
    for part in parts:
        spoken=re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"',"",part)
        repair=REPAIRED.search(spoken)
        conflicts=[name for i,name in closet.items() if i!=worn["id"] and
                   (name in spoken or (len(name)>=4 and name[:2] in spoken and re.search(r"那[件套]|衣|裤",spoken)))]
        wrong_color=any(m[2] in actual_colors and actual_colors[m[2]]!=m[1] for m in COLOR.finditer(spoken))
        implied_change=(bool(offer.get("change")) and re.search(r"换好[了啦]|换上[了啦]|穿上[了啦]|穿上试了|已经换上|刚换上",spoken)
                        and (not offer.get("target") or offer["target"]!=worn["id"]))
        if (PAST_PURCHASE.search(spoken) and CLOTHES.search(spoken) and
                (bought or (not facts.get("bought") and (f.get("intent") or f.get("resume")))) and
                not re.search(r"不是说|不等于|不能说|没查到",spoken)):
            fixed.append("今天买了"+"和".join(bought)+"。" if bought else "今天买了哪些衣服，我还没确认。")
        elif (repair and not re.search(r"还没|没能|没说|是否|要是|如果|等.{0,4}补好|补好了再|补完再",spoken)
              and ("没气" in facts.get("自行车","") or not facts.get("自行车") and
                   (f.get("intent") or f.get("resume") or f.get("outing"))) and not f.get("repair_done")):
            fixed.append("后胎还没气。" if "没气" in facts.get("自行车","") else "补胎的结果我还没确认。")
        elif ((conflicts or wrong_color or implied_change) and (worn["look"] or implied_change) and (SPOKEN_WEAR.search(spoken) or implied_change)
              and not re.search(r"还没|没穿|准备|打算|等.{0,8}(?:再|就)|如果|之前说|昨天",spoken)):
            if implied_change and f.get("change_ack")==offer.get("target") and offer.get("target"):
                fixed.append("已经开始换"+closet.get(offer["target"],"衣服")+"，还没换好。")
            elif worn["look"]:
                fixed.append("我现在穿的是"+worn["look"].split("（")[0]+"。")
            else:
                fixed.append("我还没确认换好了。")
        elif (f.get("intent") or f.get("resume") or (f.get("pending") or {}).get("stage") in ("waiting","changing","sending")) and PHOTO_DONE.search(spoken) and not re.search(r"还没|没能|等|如果",spoken) and not (
                (event.get_extra("dafeiyu_image_result") or {}).get("delivery")=="sent" or offer.get("stage")=="sent"):
            fixed.append("这张照片还没确认发出去。")
        else:
            fixed.append(part)
    if f.get("save_failed") and (LATER.search(text) or AFTER_MEAL.search(text)):
        return "这次稍后拍照的安排还没确认下来。"
    return "".join(dict.fromkeys(fixed))

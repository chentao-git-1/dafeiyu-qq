"""Small durable records scoped to the fish and the bound private conversation."""
import copy
import math
import re


def valid_photo(row):
    if row is None:
        return True
    if not isinstance(row, dict) or set(row) != {
        "id", "target", "choices", "initial", "change", "stage", "wait", "created",
        "not_before", "expires", "meal", "meal_until", "change_at"
    }:
        return False
    return (isinstance(row["id"], str) and bool(re.fullmatch(r"[a-f0-9]{24}", row["id"]))
            and type(row["change"]) is bool
            and row["stage"] in ("waiting", "changing", "sending", "held", "sent", "cancelled")
            and row["wait"] in ("ready", "meal", "later")
            and isinstance(row["choices"], list) and len(row["choices"]) <= 80
            and all(isinstance(i,str) and re.fullmatch(r"[\w-]{1,40}", i) for i in row["choices"])
            and all(isinstance(row[k],str) and len(row[k]) <= 100 for k in ("initial","target","meal"))
            and (not row["target"] or row["target"] in row["choices"])
            and all(type(row[k]) in (int,float) and math.isfinite(row[k]) for k in ("created","not_before","expires","meal_until","change_at"))
            and row["created"] <= row["not_before"] <= row["expires"])


def valid_shares(rows):
    return (isinstance(rows,list) and len(rows) <= 6
            and all(isinstance(r,dict) and set(r)=={"state","event","topic","words","stamp"}
                    and all(isinstance(r[k],str) and re.fullmatch(r"[a-f0-9]{20}",r[k]) for k in ("state","topic"))
                    and isinstance(r["event"],str) and (not r["event"] or bool(re.fullmatch(r"[a-f0-9]{20}",r["event"])))
                    and isinstance(r["words"],list) and len(r["words"])<=70
                    and all(isinstance(w,str) and re.fullmatch(r"[a-f0-9]{12}",w) for w in r["words"])
                    and type(r["stamp"]) in (int,float) and math.isfinite(r["stamp"]) for r in rows))


class Followthrough:
    def follow_scope(self,event):
        if self.storage_error or not self.store or not self.config.get("enabled",True) or not event.is_private_chat():
            return None
        state = self.store.data
        bind = state.get("binding") or {}
        if (not state.get("key_hash") or bind.get("group_id") or bind.get("umo") != event.unified_msg_origin
                or bind.get("sender_id") != str(event.get_sender_id())):
            return None
        return (bind["umo"],state["key_hash"])

    def follow_snapshot(self,event):
        scope = self.follow_scope(event)
        if not scope:
            return None
        state = self.store.data
        return scope,copy.deepcopy(state.get("outfit_photo")),copy.deepcopy(state.get("world_shares",[]))

    async def change_photo(self,event,scope,expected,new):
        if not valid_photo(new):
            return False
        async with self.lock:
            if self.follow_scope(event)!=scope or self.store.data.get("outfit_photo")!=expected:
                return False
            if new == expected:
                return True
            self.store.data["outfit_photo"]=copy.deepcopy(new)
            try:
                self._save()
            except Exception:
                self.store.data["outfit_photo"]=expected
                raise
            return True

    async def remember_share(self,event,scope,row):
        if not valid_shares([row]):
            return
        async with self.lock:
            if self.follow_scope(event)!=scope:
                return
            old=self.store.data.get("world_shares",[])
            if row in old:
                return
            self.store.data["world_shares"]=[*old[-5:],row]
            try:
                self._save()
            except Exception:
                self.store.data["world_shares"]=old
                raise

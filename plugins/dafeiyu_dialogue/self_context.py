"""Keep identity available without advertising detailed interests on every turn."""
def default_character(character):
    value=dict(character or {})
    profile=value.get('stable_profile') or {}
    value['stable_profile']={key:profile[key] for key in ('version','name','tone','judgement') if key in profile}
    value['fictional_world']=None
    value.pop('previous_story',None)
    value['details_available_on_request']=True
    return value

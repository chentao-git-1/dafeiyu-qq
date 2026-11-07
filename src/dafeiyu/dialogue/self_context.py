"""Keep identity available without advertising detailed interests on every turn."""
def default_character(character):
    value=dict(character or {})
    profile=value.get('stable_profile') or {}
    value['stable_profile']={key:profile[key] for key in ('version','name','tone','judgement') if key in profile}
    value['fictional_world']=None
    value.pop('previous_story',None)
    growth=dict(value.get('growth') or {})
    if 'global' in growth:
        state=growth['global'] or {}
        growth['global']=dict(version=state.get('version',0),tendencies=[],details_available=bool(state.get('tendencies')))
        value['growth']=growth
    value['details_available_on_request']=True
    return value


def relevant_growth(growth,query):
    from .retrieval import lexical_rank
    from .contracts import MemoryItem
    result=default_character({'growth':growth})['growth']
    rows=(growth.get('global') or {}).get('tendencies',[])
    indexed=[MemoryItem(str(i),'','','','tendency',' '.join(str(row.get(k,'')) for k in ('tendency','when','counterweight')),0,'own_history')
        for i,row in enumerate(rows)]
    if 'global' in result:
        result['global']=dict(result['global'],tendencies=[rows[int(item.id)] for item in lexical_rank(indexed,query,limit=3)])
    return result


def relevant_reflections(thoughts,query):
    """Explicit local lookup; ordinary current-intention lookup never primes old project prose."""
    from .retrieval import lexical_rank
    from .contracts import MemoryItem
    query=str(query or '').strip()
    result=dict(projects=[],reflections=[],intentions=list(thoughts.get('intentions',[]))[:1],
        older_details_available=bool(thoughts.get('projects') or thoughts.get('reflections')))
    if not query:return result
    for kind in ('projects','reflections'):
        rows=thoughts.get(kind,[]);indexed=[]
        for index,row in enumerate(rows):
            text=' '.join(str(row.get(key,'')) for key in ('title','question','insight','next_question'))
            indexed.append(MemoryItem(str(index),'','','',kind,text,float(row.get('updated') or row.get('recorded') or 0),'own_history'))
        result[kind]=[rows[int(item.id)] for item in lexical_rank(indexed,query,limit=3)]
    selected={p['id'] for p in result['projects']}
    linked=[r for r in thoughts.get('reflections',[]) if r.get('project_id') in selected]
    result['reflections']=list({r['work_id']:r for r in [*linked,*result['reflections']]}.values())[:3]
    return result

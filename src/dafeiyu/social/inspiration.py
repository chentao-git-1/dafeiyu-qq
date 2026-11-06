"""Revocable public ideas from a confirmed conversation, without private transcripts."""
import datetime as dt
import hashlib
import re

TZ = dt.timezone(dt.timedelta(hours=8))
WEEK = 7 * 86400
CONTACT = re.compile(r'https?://|[@＠]|[\w.+-]+@[\w.-]+|\d{5,}|sk-[A-Za-z0-9]|密码|验证码|私钥|api.?key|(?:token|secret)\s*[:=]', re.I)


def sharing_proposal(value, human):
    """Invalid optional metadata never prevents the conversational reply."""
    if not isinstance(value, dict):return 'none', []
    privacy=value.get('privacy', {})
    if isinstance(privacy, dict) and privacy.get('action') in ('exclude_segment','disable'):
        evidence=privacy.get('evidence')
        if (privacy.get('subject'),privacy.get('modality'))==('speaker','direct') and isinstance(evidence,str) and evidence.strip() and evidence in human:
            return privacy['action'], []
        return 'none', []
    if not isinstance(privacy,dict) or privacy.get('action','none')!='none':return 'none', []
    ideas=value.get('ideas',[])
    if not isinstance(ideas,list) or len(ideas)>2:return 'none', []
    result=[]
    for idea in ideas:
        if not isinstance(idea,dict) or idea.get('scope')!='general_topic':continue
        text,evidence=idea.get('summary'),idea.get('evidence')
        if (not isinstance(text,str) or not 6<=len(text.strip())<=240 or CONTACT.search(text)
                or not isinstance(evidence,str) or not evidence.strip() or evidence not in human):continue
        result.append(dict(summary=text.strip(),evidence_hash=hashlib.sha256(evidence.encode()).hexdigest()))
    return 'none', result


class PublicInspiration:
    def initialize_inspiration(self, db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS public_ideas (
                key TEXT PRIMARY KEY,event_key TEXT NOT NULL,user_id TEXT NOT NULL,origin TEXT NOT NULL,
                segment TEXT NOT NULL,occurred_at REAL NOT NULL,summary TEXT NOT NULL,evidence_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS public_ideas_time ON public_ideas(occurred_at);
            CREATE TABLE IF NOT EXISTS public_idea_blocks (
                user_id TEXT,origin TEXT,segment TEXT,PRIMARY KEY(user_id,origin,segment));
            CREATE TABLE IF NOT EXISTS public_idea_epochs (user_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS public_idea_schema (version INTEGER PRIMARY KEY);
        ''')
        # Preserve old refusals once, without continuing the old sensitive-word filter.
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM public_idea_schema WHERE version=1').fetchone():
            db.execute('INSERT OR IGNORE INTO public_idea_blocks SELECT user_id,origin,segment FROM public_topic_segments WHERE blocked=1')
            db.execute('INSERT INTO public_idea_schema VALUES (1)')

    @staticmethod
    def inspiration_epoch(db, uid):
        row=db.execute('SELECT revision FROM public_idea_epochs WHERE user_id=?',(str(uid),)).fetchone()
        return row[0] if row else 0

    def revoke_public_ideas(self, uid, origin=None, *, segment=None, disable=False):
        """Forget derivatives/withdraw permission; confirmed public posts are separate."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO public_idea_epochs VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1',(str(uid),))
            where='user_id=?';params=[str(uid)]
            if origin is not None:where+=' AND origin=?';params.append(origin)
            if segment is not None:
                where+=' AND segment=?';params.append(segment)
                db.execute('INSERT OR IGNORE INTO public_idea_blocks VALUES (?,?,?)',(str(uid),origin,segment))
            for table in ('public_ideas','public_topic_candidates'):
                db.execute('DELETE FROM '+table+' WHERE '+where,params)
            if disable:
                db.execute('INSERT OR REPLACE INTO public_summary_preferences VALUES (?,1)',(str(uid),))

    def record_public_ideas(self, source, ideas):
        if not source or source.get('media') or not ideas:return 0
        now=self.clock();uid=str(source['user_id'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM public_ideas WHERE occurred_at<=?',(now-WEEK,))
            if source.get('public_epoch',0)!=self.inspiration_epoch(db,uid):return 0
            row=db.execute('SELECT * FROM dialogue_turns WHERE event_key=?',(source['event_key'],)).fetchone()
            if not row or row['user_id']!=uid or row['occurred_at']!=source['occurred_at'] or not now-WEEK<row['occurred_at']<=now+5:return 0
            if db.execute('SELECT 1 FROM public_summary_preferences WHERE user_id=? AND disabled=1',(uid,)).fetchone():return 0
            if db.execute('SELECT 1 FROM public_idea_blocks WHERE user_id=? AND origin=? AND segment=?',(uid,source['origin'],source['segment'])).fetchone():return 0
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='bots'").fetchone() and db.execute('SELECT 1 FROM bots WHERE qq=?',(uid,)).fetchone():return 0
            added=0
            for idea in ideas[:2]:
                key='idea:'+hashlib.sha256((source['event_key']+'\0'+idea['summary']).encode()).hexdigest()
                added+=db.execute('INSERT OR IGNORE INTO public_ideas VALUES (?,?,?,?,?,?,?,?)',
                    (key,source['event_key'],uid,source['origin'],source['segment'],source['occurred_at'],idea['summary'],idea['evidence_hash'])).rowcount
            return added

    def inspiration_topics(self, now):
        with self.connect() as db:
            bots={str(r[0]) for r in db.execute('SELECT qq FROM bots')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='bots'").fetchone() else set()
            rows=db.execute('''SELECT i.* FROM public_ideas i
                LEFT JOIN public_summary_preferences p ON i.user_id=p.user_id
                WHERE i.occurred_at>? AND i.occurred_at<=? AND COALESCE(p.disabled,0)=0
                AND NOT EXISTS(SELECT 1 FROM public_idea_blocks b WHERE b.user_id=i.user_id AND b.origin=i.origin AND b.segment=i.segment)
                ORDER BY i.occurred_at DESC,i.key''',(now-WEEK,now)).fetchall()
        result=[];seen=set()
        for row in rows:
            if row['user_id'] in bots or row['summary'] in seen:continue
            result.append(dict(key=row['key'],date=dt.datetime.fromtimestamp(row['occurred_at'],TZ).date().isoformat(),summary=row['summary']))
            seen.add(row['summary'])
            if len(result)==8:break
        return result

    def inspiration_valid(self, db, topic, bots):
        row=db.execute('''SELECT i.*,p.disabled FROM public_ideas i
            LEFT JOIN public_summary_preferences p ON i.user_id=p.user_id WHERE i.key=?
            AND NOT EXISTS(SELECT 1 FROM public_idea_blocks b WHERE b.user_id=i.user_id AND b.origin=i.origin AND b.segment=i.segment)''',(topic.get('key'),)).fetchone()
        return bool(row and not row['disabled'] and row['user_id'] not in bots
            and self.clock()-WEEK<row['occurred_at']<=self.clock()+5
            and topic.get('summary')==row['summary']
            and topic.get('date')==dt.datetime.fromtimestamp(row['occurred_at'],TZ).date().isoformat())

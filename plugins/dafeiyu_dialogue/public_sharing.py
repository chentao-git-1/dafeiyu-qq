"""Join the existing generation/delivery lifecycle to public inspiration."""
import asyncio
from astrbot.api import logger
from dafeiyu.social.activity import ActivityStore
from dafeiyu.social.inspiration import sharing_proposal


class PublicSharing:
    def public_activity(self):
        path=self.root/'plugin_data/dafeiyu_social/preferences.db'
        return ActivityStore(path) if path.is_file() else None

    def purge_public_drafts(self, activity):
        for path in (self.root/'plugin_data/astrbot_plugin_qzone').glob('posts_*.db'):
            activity.purge_drafts(path)

    def revoke_public_inspiration(self, owner, origin=None):
        activity=self.public_activity()
        if activity:
            activity.revoke_public_ideas(owner,origin)
            self.purge_public_drafts(activity)

    async def prepare_public_sharing(self, event, generated, turn, human, privacy):
        event.set_extra('dafeiyu_public_ideas',[])
        source=event.get_extra('dafeiyu_activity_input',{}) or {}
        if source.get('user_id')!=turn.actor or source.get('origin')!=turn.origin:return
        try:
            action,ideas=sharing_proposal(generated.sharing,human)
            activity=self.public_activity()
            if activity is None:return
            if action!='none':
                await asyncio.to_thread(activity.revoke_public_ideas,turn.actor,
                    None if action=='disable' else turn.origin,
                    segment=source['segment'] if action=='exclude_segment' else None,disable=action=='disable')
                await asyncio.to_thread(self.purge_public_drafts,activity)
                event.set_extra('dafeiyu_public_privacy',action)
            elif privacy['action']=='none' and not event.get_extra('dafeiyu_semantic_forgetting',False):
                event.set_extra('dafeiyu_public_ideas',ideas)
        except Exception as error:
            logger.warning('Public inspiration unavailable: %s',type(error).__name__)

    def commit_public_sharing(self, event, source):
        ideas=event.get_extra('dafeiyu_public_ideas',[])
        activity_source=event.get_extra('dafeiyu_activity_input',{}) or {}
        if (not ideas or not event.get_extra('dafeiyu_generation_valid',False)
                or activity_source.get('user_id')!=source['owner']
                or activity_source.get('origin')!=source['origin']):return 0
        activity=self.public_activity()
        if activity is None:return 0
        # Delivery hook normally recorded this; a repeated receipt is idempotent.
        activity.record_confirmed(activity_source)
        return activity.record_public_ideas(activity_source,ideas)

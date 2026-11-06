"""Explicit evidence about the bot; missing progress never means an invented activity."""
import math


def self_state(completed=None, progress=None, works=()):
    verified = None
    if (isinstance(completed, dict) and completed.get('id') and completed.get('day')
            and isinstance(completed.get('completed_at'), (int, float))
            and not isinstance(completed['completed_at'], bool)
            and math.isfinite(completed['completed_at']) and completed['completed_at'] > 0):
        verified = {k: completed[k] for k in ('id', 'day', 'kind', 'note', 'thought',
                    'project', 'stage', 'source', 'completed_at') if k in completed}
    ongoing = None
    if (isinstance(progress, dict) and progress.get('status') == 'started'
            and progress.get('kind') in ('reading', 'writing', 'drawing') and progress.get('day')):
        ongoing = {k: progress[k] for k in ('day', 'kind', 'status', 'updated') if k in progress}
    return {'verified_completed': verified, 'verified_in_progress': ongoing,
        'conversation_works':list(works),
        'progress_knowledge': 'confirmed_run' if ongoing else 'no_confirmed_current_run',
        'unverified_past_assistant_descriptions_are_not_evidence': True,
        'thoughts_are_not_actions': True, 'fiction_is_not_real_activity': True}

"""One entry for episode and exchange operations; existing stores stay separate."""
from .state import forbidden


class MemoryCoordinator:
    def __init__(self, episodes, exchanges):
        self.episodes = episodes
        self.exchanges = exchanges  # Lazy: an episode lookup need not open the exchange DB.

    def relevant(self, text, style, assistant_tail):
        result = []
        for row in self.episodes.search(limit=100):
            if row['kind'] == 'joke' and (style == 'gentle' or row['topic'] in assistant_tail):
                continue
            if row['topic'] in text and not forbidden(text):
                result.append(self.episodes.view(row))
            if len(result) >= 3:
                break
        return result

    def search(self, query, owner):
        return [self.episodes.view(row) for row in self.episodes.search(str(query)[:80], owner, 5)]

    def forget_turn(self, actor, origin, text):
        return self.exchanges().observe_forgetting(actor, origin, text)

    def recall_turn(self, actor, origin, text, actual_history, diagnostics=None):
        exclude = [row['text'] for row in actual_history if row.get('speaker') == '鲸鲸']
        kwargs = {'diagnostics': diagnostics} if diagnostics is not None else {}
        return self.exchanges().recall(actor, origin, text, exclude=exclude, **kwargs)

    def forget_episode(self, actor, episode_id):
        row = self.episodes.get(episode_id)
        # Episodes retains its existing ownership checks before any other deletion.
        self.episodes.forget(actor, episode_id)
        if row:
            self.exchanges().forget(actor, None, row['topic'])
        with self.episodes.connect() as db:
            db.execute('DELETE FROM offers WHERE id=?', (episode_id,))

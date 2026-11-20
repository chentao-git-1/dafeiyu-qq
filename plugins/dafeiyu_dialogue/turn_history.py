"""Retain whole conversation turns and keep their prefix through tool calls."""

from astrbot.core.agent.context.truncator import ContextTruncator
from astrbot.core.agent.message import Message


class DialogueTurnTruncator(ContextTruncator):
    """Apply the history window once per runner reset."""

    def __init__(self) -> None:
        self._applied = False

    def truncate_by_turns(
        self,
        messages: list[Message],
        keep_most_recent_turns: int,
        drop_turns: int = 1,
    ) -> list[Message]:
        if self._applied or keep_most_recent_turns < 0:
            return messages
        self._applied = True

        user_starts = [index for index, message in enumerate(messages) if message.role == "user"]
        if not user_starts or len(user_starts) <= keep_most_recent_turns:
            return messages

        turns_to_keep = max(1, keep_most_recent_turns - max(1, drop_turns) + 1)
        system_messages, _ = self._split_system_rest(messages)
        return self.fix_messages(system_messages + messages[user_starts[-turns_to_keep]:])

"""Trusted identities and attributed evidence, independent of AstrBot imports."""
from dataclasses import asdict, dataclass
import math


ACTIVE_KEY = 'dafeiyu_dialogue_v2'


@dataclass(frozen=True)
class TurnContext:
    actor: str
    origin: str
    platform: str
    message_id: str
    observed_at: float

    @property
    def is_group(self):return self.origin.startswith(self.platform+':GroupMessage:')

    def __post_init__(self):
        private=self.origin==self.platform+':FriendMessage:'+self.actor
        group=self.origin.startswith(self.platform+':GroupMessage:') and self.origin[len(self.platform+':GroupMessage:'):].isdigit()
        if (not self.actor.isdigit() or not (private or group)
                or isinstance(self.observed_at, bool) or not math.isfinite(self.observed_at)):
            raise ValueError('Invalid conversation identity')


@dataclass(frozen=True)
class MemoryItem:
    id: str
    owner: str
    origin: str
    visibility: str
    kind: str
    text: str
    recorded_at: float
    source: str
    scope_id: str = ''
    occurred_at: float | None = None
    status: str = ''

    def visible_to(self, turn):
        if turn.is_group:
            return (self.origin==turn.origin and (self.visibility=='group'
                or self.visibility=='shared' and self.scope_id in (turn.platform+':shared','legacy:shared')
                or self.visibility=='session' and self.owner==turn.actor))
        if self.visibility=='group':return False
        if self.visibility == 'shared':
            return self.scope_id in (turn.platform + ':shared', 'legacy:shared')
        return (self.visibility == 'session' and self.owner == turn.actor
                and self.origin == turn.origin)

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class CatalogSnapshot:
    items: tuple[MemoryItem, ...]
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    kind: str
    target: str
    status: str
    evidence_id: str = ''

    def supports(self, kind, target):
        return (self.kind == kind and self.target == target and self.status == 'confirmed'
                and bool(self.action_id and self.evidence_id))

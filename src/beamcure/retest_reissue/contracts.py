from dataclasses import dataclass, replace

# 撤回原因码：同版冲突 / 失去复压资格 / 用量变化
REVOKE_VERSION_CONFLICT = 'VERSION_CONFLICT'
REVOKE_ELIGIBILITY_LOST = 'ELIGIBILITY_LOST'
REVOKE_LOAD_CHANGED = 'LOAD_CHANGED'
REVOKE_CODES = frozenset((REVOKE_VERSION_CONFLICT, REVOKE_ELIGIBILITY_LOST, REVOKE_LOAD_CHANGED))

# 续派凭证沿用首次签发的特征标准、模型与校准，冻结版本不随补测改变。
@dataclass(frozen=True, slots=True)
class FrozenReference:
    feature_stamp: str
    model_name: str
    model_fingerprint: str
    calibration: str

    def __post_init__(self):
        if not self.feature_stamp or not self.model_name or not self.model_fingerprint or not self.calibration:
            raise ValueError('续派必须携带首次签发的冻结凭证')


@dataclass(frozen=True, slots=True)
class Supplement:
    """同批补测：同一单号的新版量测。event_id 用于幂等，重试不重复扣工位。"""
    event_id: str
    key: str
    revision: int
    measures: tuple[float, ...]
    estimate: float
    missing: tuple[str, ...] = ()
    conflict: bool = False

    def __post_init__(self):
        if not self.event_id or not self.key:
            raise ValueError('补测事件缺少身份标识')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('补测版次须为正整数')
        if len(self.measures) != 6:
            raise ValueError('量测必须包含六项读数')
        if not 0.0 <= self.estimate <= 1.0:
            raise ValueError('补测风险须为 0 至 1 的概率')


@dataclass(frozen=True, slots=True)
class Ticket:
    """一张复压签发单的生命周期快照。"""
    key: str
    cohort: str
    revision: int
    measures: tuple[float, ...]
    estimate: float
    cost: int
    eligible: bool
    state: str            # 已上机 / 已签发 / 候补 / 已撤回
    reason: str
    series: int            # 凭证序号，1 为首次签发，其后为续派
    event_id: str | None = None

    def with_state(self, *, state, reason=None, series=None, event_id=None):
        return replace(self, state=state,
                       reason=self.reason if reason is None else reason,
                       series=self.series if series is None else series,
                       event_id=self.event_id if event_id is None else event_id)


@dataclass(frozen=True, slots=True)
class Voucher:
    """原凭证或续派凭证，二者可在工作台对照。"""
    series: int
    kind: str              # 原凭证 / 续派凭证
    event_id: str | None
    reference: FrozenReference
    issued: tuple[str, ...]
    revoked: tuple[tuple[str, str], ...]
    promoted: tuple[str, ...]
    capacity: int
    used: int

    def __post_init__(self):
        if self.used > self.capacity:
            raise ArithmeticError('凭证工位用量超出本班额度')
        if self.kind not in ('原凭证', '续派凭证'):
            raise ValueError('未知凭证类型')


@dataclass(frozen=True, slots=True)
class ReissueBoard:
    reference: FrozenReference
    capacity: int
    tickets: tuple[Ticket, ...]
    vouchers: tuple[Voucher, ...]
    consumed_events: tuple[str, ...]

    def ticket(self, key):
        for item in self.tickets:
            if item.key == key:
                return item
        raise KeyError(key)

    def used_slots(self):
        return sum(item.cost for item in self.tickets if item.state in ('已上机', '已签发'))

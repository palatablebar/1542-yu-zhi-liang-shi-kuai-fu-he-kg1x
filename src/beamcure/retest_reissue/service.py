from dataclasses import replace

from .contracts import (FrozenReference, ReissueBoard, Supplement, Ticket, Voucher,
                        REVOKE_VERSION_CONFLICT, REVOKE_ELIGIBILITY_LOST, REVOKE_LOAD_CHANGED)
from ..retest_press import service as press
from ..retest_press.contracts import RISK_CUTOFF

SLOT_STATES = ('已上机', '已签发')
OPEN_STATES = SLOT_STATES + ('候补',)


def eligibility(measures, estimate, missing=()):
    """沿用首次签发的硬规则与模型阈值判定复压资格。"""
    hits = press.signals(tuple(measures), estimate, tuple(missing))
    hard = bool(hits and hits[0].hard)
    qualified = bool(hits) or estimate >= RISK_CUTOFF
    return (not hard) and qualified, hard, tuple(item.rule_id for item in hits)


def load_of(measures):
    # 工位用量口径与首次签发完全一致：1 + int(probe_gap >= 12)
    return press.expected_load(tuple(measures))


def _queue_rank(ticket):
    physical = press.dispatch_pressure(ticket.measures, ticket.estimate)
    category = press.cube_load(ticket.measures)
    return (-physical, category, -ticket.estimate, ticket.key)


def open_board(jobs, measures_by_key, reference, capacity):
    """从首次签发方案开立续派工作台。

    jobs 为 retest_press.plan_jobs 产出的 Job；measures_by_key 提供每张单的六向量测。
    原凭证原样继承首次签发的占用与候补，补位只在续派事件发生时进行。
    """
    if not isinstance(reference, FrozenReference):
        raise ValueError('续派工作台需要首次签发的冻结凭证')
    tickets = []
    for job in jobs:
        measures = tuple(measures_by_key[job.key])
        if len(measures) != 6:
            raise ValueError('首次签发票据量测不完整')
        if job.state == '已分派':
            state = '已签发'
        elif job.state in ('候补', '暂停', '人工判读', '观察'):
            state = job.state
        else:
            raise ValueError(f'未知首次签发状态: {job.state}')
        tickets.append(Ticket(job.key, job.cohort, 1, measures, job.estimate, job.cost,
                              state in OPEN_STATES, state, job.reason, 1))
    used = sum(item.cost for item in tickets if item.state in SLOT_STATES)
    if used > capacity:
        raise ArithmeticError('首次签发用量超出本班额度')
    original = Voucher(1, '原凭证', None, reference,
                       tuple(item.key for item in tickets if item.state in SLOT_STATES),
                       (), (), capacity, used)
    return ReissueBoard(reference, capacity, tuple(tickets), (original,), ())


def _settle(board, withdrawn, promoted, event_id, slots_freed=0):
    """撤回释放工位时，按新风险重排候补并补派，生成续派凭证。

    只使用本轮新释放的工位 slots_freed；既有的闲置额度不主动占用，
    没有撤回空位时候补保持原序。
    """
    tickets = list(board.tickets)
    remaining = board.capacity - sum(item.cost for item in tickets if item.state in SLOT_STATES)
    fillable = min(slots_freed, remaining)
    promoted_keys = list(promoted)
    if fillable > 0:
        waiting = sorted((item for item in tickets if item.state == '候补'), key=_queue_rank)
        for ticket in waiting:
            if fillable <= 0:
                break
            if ticket.cost <= fillable and ticket.eligible:
                index = next(i for i, item in enumerate(tickets) if item.key == ticket.key)
                tickets[index] = ticket.with_state(state='已签发', reason=ticket.reason)
                fillable -= ticket.cost
                remaining -= ticket.cost
                promoted_keys.append(ticket.key)
    used = board.capacity - remaining
    series = (board.vouchers[-1].series + 1) if board.vouchers else 1
    voucher = Voucher(series, '续派凭证' if board.vouchers else '原凭证', event_id, board.reference,
                      tuple(item.key for item in tickets if item.state in SLOT_STATES),
                      tuple(sorted(withdrawn)), tuple(promoted_keys), board.capacity, used)
    return ReissueBoard(board.reference, board.capacity, tuple(tickets),
                        board.vouchers + (voucher,),
                        tuple(sorted(set(board.consumed_events) | {event_id})))


def apply_supplement(board, supplement):
    """处理一张同批补测，重算风险并维持工位守恒。重试同一 event_id 直接回放。"""
    if not isinstance(board, ReissueBoard):
        raise ValueError('续派只能作用于已开立的工作台')
    if not isinstance(supplement, Supplement):
        raise ValueError('补测事件格式不符')
    if supplement.event_id in board.consumed_events:
        return board  # 重试同批补测：幂等回放，不重复扣工位
    try:
        index = next(i for i, item in enumerate(board.tickets) if item.key == supplement.key)
    except StopIteration:
        raise KeyError(f'补测单 {supplement.key} 不在本班台账') from None
    current = board.tickets[index]
    if current.state == '已撤回' and not supplement.conflict:
        raise ValueError('已撤回单据须先人工处理，不能由补测自动复活')

    tickets = list(board.tickets)
    withdrawn = []
    slots_freed = 0
    new_cost = load_of(supplement.measures)
    qualified, hard, hits = eligibility(supplement.measures, supplement.estimate, supplement.missing)

    if supplement.conflict:
        # 同版冲突：无论是否占工位都撤回，空位留给后续补派
        if current.state in SLOT_STATES:
            slots_freed += current.cost
        tickets[index] = current.with_state(state='已撤回', reason=REVOKE_VERSION_CONFLICT)
        withdrawn.append((current.key, REVOKE_VERSION_CONFLICT))
    elif current.state in SLOT_STATES:
        if not qualified:
            reason = REVOKE_ELIGIBILITY_LOST
            slots_freed += current.cost
            tickets[index] = current.with_state(state='已撤回', reason=reason)
            withdrawn.append((current.key, reason))
        elif new_cost != current.cost:
            reason = REVOKE_LOAD_CHANGED
            slots_freed += current.cost
            tickets[index] = current.with_state(state='已撤回', reason=reason)
            withdrawn.append((current.key, reason))
        else:
            # 仍符合复压条件且用量没变：已签发单照常上机，仅刷新风险，不动工位
            tickets[index] = replace(current, revision=supplement.revision, measures=supplement.measures,
                                     estimate=supplement.estimate, eligible=True)
    else:
        refreshed = replace(current, revision=supplement.revision, measures=supplement.measures,
                            estimate=supplement.estimate, cost=new_cost,
                            eligible=qualified,
                            reason=hits[0] if hits else ('ABOVE_TRIGGER' if qualified else current.reason))
        if qualified:
            # 高风险梁批进入候补队列，由 _settle 按新风险统一排序，尽快复压
            tickets[index] = refreshed.with_state(state='候补', reason=refreshed.reason)
        elif hard:
            tickets[index] = refreshed.with_state(state='暂停', reason=hits[0])
        else:
            tickets[index] = refreshed.with_state(state='观察', reason='BELOW_TRIGGER')

    staged = ReissueBoard(board.reference, board.capacity, tuple(tickets),
                          board.vouchers, board.consumed_events)
    return _settle(staged, withdrawn, [], supplement.event_id, slots_freed)


def apply_supplements(board, supplements):
    for supplement in supplements:
        board = apply_supplement(board, supplement)
    return board


def mark_pressed(board, key):
    """已签发单上机：物理占用压机，工位额度不变。"""
    index = next((i for i, item in enumerate(board.tickets) if item.key == key), -1)
    if index < 0:
        raise KeyError(f'单据 {key} 不在本班台账')
    current = board.tickets[index]
    if current.state != '已签发':
        raise ValueError('只有已签发单据可以上机')
    tickets = list(board.tickets)
    tickets[index] = current.with_state(state='已上机')
    return ReissueBoard(board.reference, board.capacity, tuple(tickets),
                        board.vouchers, board.consumed_events)


def voucher_trace(board):
    """工作台对照原凭证与续派凭证：列出每张单的凭证轨迹。"""
    trace = {}
    for voucher in board.vouchers:
        for key in voucher.issued:
            trace.setdefault(key, []).append({'凭证': voucher.kind, '序号': voucher.series,
                                              '事件': voucher.event_id, '处置': '占用工位'})
        for key, code in voucher.revoked:
            trace.setdefault(key, []).append({'凭证': voucher.kind, '序号': voucher.series,
                                              '事件': voucher.event_id, '处置': '撤回', '原因': code})
        for key in voucher.promoted:
            trace.setdefault(key, []).append({'凭证': voucher.kind, '序号': voucher.series,
                                              '事件': voucher.event_id, '处置': '候补补派'})
    return trace


def workbench_view(board):
    """供工作台展示：当前票据状态、用量与凭证对照。"""
    rows = []
    trace = voucher_trace(board)
    for ticket in sorted(board.tickets, key=lambda item: (item.state not in SLOT_STATES, -item.estimate, item.key)):
        rows.append({'单号': ticket.key, '作业区': ticket.cohort, '版次': ticket.revision,
                     '状态': ticket.state, '用量': ticket.cost, '风险': round(ticket.estimate, 4),
                     '占用工位': ticket.state in SLOT_STATES, '凭证轨迹': trace.get(ticket.key, [])})
    return {'额度': board.capacity, '已用工位': board.used_slots(),
            '剩余工位': board.capacity - board.used_slots(), '票据': rows,
            '凭证数': len(board.vouchers), '冻结标准': board.reference.feature_stamp,
            '冻结模型': board.reference.model_fingerprint, '冻结校准': board.reference.calibration}

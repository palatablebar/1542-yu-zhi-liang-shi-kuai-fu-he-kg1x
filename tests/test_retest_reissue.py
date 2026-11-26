import pytest

from beamcure.retest_press.contracts import Job
from beamcure.retest_reissue import (
    FrozenReference, Supplement, open_board, apply_supplement, apply_supplements,
    mark_pressed, eligibility, load_of, voucher_trace, workbench_view,
    REVOKE_VERSION_CONFLICT, REVOKE_ELIGIBILITY_LOST, REVOKE_LOAD_CHANGED,
)

REFERENCE = FrozenReference('feat-2026.11', 'L2 试块风险分类', 'model-abc123', 'press_scale#9')
MEASURE_KEYS = ('cure_min', 'chamber_c', 'binder_kg', 'water_pct', 'probe_gap', 'age_hours')


def measures(probe_gap=10.0, **overrides):
    base = dict(cure_min=320.0, chamber_c=52.0, binder_kg=330.0,
                water_pct=40.0, probe_gap=probe_gap, age_hours=22.0)
    base.update(overrides)
    return tuple(base[key] for key in MEASURE_KEYS)


def make_job(key, state='已分派', reason='ABOVE_TRIGGER', estimate=0.7, cost=None, cohort='甲区'):
    data = measures()
    if cost is None:
        cost = load_of(data)
    return Job(key, cohort, estimate, None, None, cost, state, reason, '测试处置', 0.6, ())


def board_with(jobs, capacity=6):
    table = {job.key: measures() for job in jobs}
    return open_board(jobs, table, REFERENCE, capacity)


def supp(key, estimate=0.72, *, event='ev-1', revision=2, conflict=False, **overrides):
    return Supplement(event, key, revision, measures(**overrides), estimate, (), conflict)


def test_open_board_seals_original_voucher_and_keeps_capacity():
    board = board_with([make_job('A'), make_job('B', state='候补', cost=1)], capacity=6)
    assert board.vouchers[0].kind == '原凭证'
    assert board.vouchers[0].issued == ('A',)  # 原凭证原样继承首次方案
    assert board.used_slots() == 1 and board.capacity == 6
    view = workbench_view(board)
    assert view['冻结标准'] == 'feat-2026.11' and view['凭证数'] == 1


def test_reissue_keeps_frozen_reference_through_every_voucher():
    board = board_with([make_job('A'), make_job('B', state='候补')])
    board = apply_supplement(board, supp('A', estimate=0.74))
    stamps = {(v.reference.feature_stamp, v.reference.model_fingerprint, v.reference.calibration)
              for v in board.vouchers}
    assert stamps == {('feat-2026.11', 'model-abc123', 'press_scale#9')}


def test_unchanged_eligibility_and_load_keeps_issued_ticket_on_press():
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=4)
    board = mark_pressed(board, 'A')
    before = board.used_slots()
    board = apply_supplement(board, supp('A', estimate=0.9, probe_gap=10.5))  # 用量仍为 1、仍合格
    ticket = board.ticket('A')
    assert ticket.state == '已上机' and ticket.revision == 2
    assert board.used_slots() == before  # 照常上机，不腾位、不重扣
    assert board.ticket('A').cost == 1


def test_version_conflict_revokes_even_when_occupied():
    board = board_with([make_job('A', cost=2), make_job('B', state='候补', estimate=0.8, cost=1)])
    board = apply_supplement(board, supp('A', conflict=True, event='ev-c'))
    assert board.ticket('A').state == '已撤回'
    assert board.ticket('A').reason == REVOKE_VERSION_CONFLICT
    latest = board.vouchers[-1]
    assert ('A', REVOKE_VERSION_CONFLICT) in latest.revoked


def test_lost_eligibility_revokes_and_frees_slot():
    # 新量测落到阈值以下且无规则命中：失去复压资格
    mild = measures(probe_gap=6.0, chamber_c=45.0, water_pct=35.0, age_hours=26.0, cure_min=400.0)
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', estimate=0.81, cost=1)], capacity=4)
    board = apply_supplement(board, Supplement('ev-e', 'A', 2, mild, 0.2))
    assert board.ticket('A').state == '已撤回'
    assert board.ticket('A').reason == REVOKE_ELIGIBILITY_LOST
    assert board.used_slots() <= board.capacity


def test_changed_load_revokes_issued_ticket():
    board = board_with([make_job('A', cost=1)], capacity=6)
    assert load_of(measures(probe_gap=15.0)) == 2
    board = apply_supplement(board, supp('A', estimate=0.8, probe_gap=15.0, event='ev-l'))
    assert board.ticket('A').state == '已撤回'
    assert board.ticket('A').reason == REVOKE_LOAD_CHANGED


def test_freed_slot_is_refilled_by_new_risk_order():
    waiting = [make_job('W1', state='候补', estimate=0.55, cost=1),
               make_job('W2', state='候补', estimate=0.91, cost=1)]
    board = board_with([make_job('A', cost=2), *waiting], capacity=3)
    assert board.used_slots() == 2  # A 占 2，候补无机可上
    mild = measures(probe_gap=6.0, chamber_c=44.0, water_pct=34.0, age_hours=27.0, cure_min=420.0)
    board = apply_supplement(board, Supplement('ev-r', 'A', 2, mild, 0.15))
    assert board.ticket('A').state == '已撤回'
    # 高风险 W2 应排在 W1 前面补派
    assert board.ticket('W2').state == '已签发'
    latest = board.vouchers[-1]
    assert latest.promoted[0] == 'W2' and latest.used <= latest.capacity


def test_high_risk_supplement_jumps_waiting_queue():
    board = board_with([make_job('A', cost=1),
                        make_job('Q1', state='候补', estimate=0.55, cost=1),
                        make_job('Q2', state='候补', estimate=0.6, cost=1)], capacity=1)
    # 撤回 A 放出一个工位
    mild = measures(probe_gap=6.0, chamber_c=44.0, water_pct=34.0, age_hours=27.0, cure_min=420.0)
    board = apply_supplement(board, Supplement('ev-x', 'A', 2, mild, 0.1))
    # 候补里没有新增高风险单，按既有风险 Q2 先上
    assert board.ticket('Q2').state == '已签发' and board.ticket('Q1').state == '候补'
    # Q1 收到补测后风险飙升，应在下次空位出现时排到最前
    board = apply_supplement(board, supp('Q2', estimate=0.12, event='ev-drop',
                                         probe_gap=6.0, chamber_c=44.0, water_pct=34.0,
                                         age_hours=27.0, cure_min=420.0))
    assert board.ticket('Q2').state == '已撤回'
    assert board.ticket('Q1').state == '已签发'


def test_retrying_same_event_is_idempotent_and_does_not_recharge():
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=4)
    event = supp('A', estimate=0.66, event='ev-dup')
    once = apply_supplement(board, event)
    twice = apply_supplement(once, event)
    assert twice == once
    assert twice.consumed_events.count('ev-dup') == 1
    assert len(twice.vouchers) == len(once.vouchers)


def test_slot_conservation_under_mixed_supplements():
    board = board_with([make_job('A', cost=1), make_job('B', cost=2),
                        make_job('C', state='候补', estimate=0.85, cost=1)], capacity=4)
    mild = measures(probe_gap=6.0, chamber_c=44.0, water_pct=34.0, age_hours=27.0, cure_min=420.0)
    board = apply_supplements(board, [
        Supplement('ev-1', 'A', 2, mild, 0.1),                    # 失格撤回，放 1 位
        supp('B', estimate=0.9, probe_gap=15.0, event='ev-2'),   # 用量 1->2 撤回
        supp('C', estimate=0.95, event='ev-3'),                  # 高风险候补
    ])
    occupied = sum(t.cost for t in board.tickets if t.state in ('已上机', '已签发'))
    assert occupied <= board.capacity
    assert board.ticket('C').state == '已签发'


def test_workbench_matches_original_and_continuation_vouchers():
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=3)
    mild = measures(probe_gap=6.0, chamber_c=44.0, water_pct=34.0, age_hours=27.0, cure_min=420.0)
    board = apply_supplement(board, Supplement('ev-v', 'A', 2, mild, 0.1))
    trace = voucher_trace(board)
    kinds = [step['凭证'] for step in trace['A']]
    assert '原凭证' in kinds and '续派凭证' in kinds
    assert any(step.get('原因') == REVOKE_ELIGIBILITY_LOST for step in trace['A'])
    view = workbench_view(board)
    assert view['已用工位'] + view['剩余工位'] == view['额度']


def test_hard_rule_supplement_parks_ticket_without_slot_charge():
    # 热梯度硬规则：HEAT_GRADIENT
    hot = measures(probe_gap=18.0, chamber_c=64.0)
    qualified, hard, hits = eligibility(hot, 0.9)
    assert hard and not qualified and hits[0] == 'HEAT_GRADIENT'
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=3)
    board = apply_supplement(board, Supplement('ev-h', 'B', 2, hot, 0.9))
    assert board.ticket('B').state == '暂停'
    assert board.used_slots() == 1  # 仅 A 占工位


def test_conflicted_ticket_cannot_silently_reactivate():
    board = board_with([make_job('A', cost=1)], capacity=3)
    board = apply_supplement(board, supp('A', conflict=True, event='ev-c1'))
    with pytest.raises(ValueError, match='复活'):
        apply_supplement(board, supp('A', event='ev-c2'))


def test_unknown_supplement_ticket_rejected():
    board = board_with([make_job('A')])
    with pytest.raises(KeyError):
        apply_supplement(board, supp('GHOST'))


def test_idle_capacity_is_not_filled_without_a_withdrawn_slot():
    # 容量 4，A 占 1，候补 B/C 留队；对 A 的合规补测不产生空位，候补不得被补派
    board = board_with([make_job('A', cost=1),
                        make_job('B', state='候补', estimate=0.9, cost=1),
                        make_job('C', state='候补', estimate=0.85, cost=1)], capacity=4)
    assert board.ticket('B').state == '候补' and board.ticket('C').state == '候补'
    board = apply_supplement(board, supp('A', estimate=0.72, event='ev-idle'))
    assert board.ticket('A').state == '已签发'
    assert board.ticket('B').state == '候补' and board.ticket('C').state == '候补'
    assert board.vouchers[-1].promoted == () and board.used_slots() == 1


def test_version_conflict_revokes_waiting_ticket_too():
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=3)
    board = apply_supplement(board, supp('B', conflict=True, event='ev-wc'))
    assert board.ticket('B').state == '已撤回'
    assert board.ticket('B').reason == REVOKE_VERSION_CONFLICT
    # 冲突撤回不产生补派（B 本就不占工位，没有释放新工位）
    assert board.vouchers[-1].promoted == ()


def test_only_issued_ticket_can_be_pressed():
    # 容量被 A 占满，B 只能候补，因此无空位可补派
    board = board_with([make_job('A', cost=1), make_job('B', state='候补', cost=1)], capacity=1)
    assert board.ticket('B').state == '候补'
    with pytest.raises(ValueError, match='上机'):
        mark_pressed(board, 'B')


def test_supplement_validates_probability_and_measures():
    with pytest.raises(ValueError, match='概率'):
        Supplement('ev', 'A', 2, measures(), 1.5)
    with pytest.raises(ValueError, match='六项'):
        Supplement('ev', 'A', 2, (1.0, 2.0), 0.5)


def test_board_rejects_reference_without_frozen_credentials():
    with pytest.raises(ValueError, match='冻结凭证'):
        open_board([], {}, FrozenReference('', 'm', 'f', 'c'), 4)

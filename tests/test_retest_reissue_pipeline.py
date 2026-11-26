"""续派工作台与真实训练管线的端到端衔接。

首次签发的特征标准、模型、校准来自完整管线，续派全程沿用，不重新拟合。
"""
from dataclasses import replace

from beamcure.pipeline import ShiftDossier, open_fixture
from beamcure.retest_press import service as dispatch
from beamcure.retest_reissue import (
    FrozenReference, Supplement, open_board, apply_supplement, mark_pressed,
    workbench_view, voucher_trace,
)

MEASURE_ORDER = ('cure_min', 'chamber_c', 'binder_kg', 'water_pct', 'probe_gap', 'age_hours')


def _build_board(capacity=8, mode='balanced'):
    doc = open_fixture()
    dossier = ShiftDossier(doc['records'], doc['as_of'])
    calibration, predictions = dossier.grading()
    _, checked_live, drift = dossier.verification(predictions, calibration)
    plan = dispatch.plan_jobs(checked_live, drift.domain, capacity, mode)
    vectors = {point.key: point for point in predictions['live']}
    measures_by_key = {job.key: vectors[job.key].input_values for job in plan.jobs}
    reference = FrozenReference(dossier.feature_stamp.fingerprint,
                                dossier.forecaster.name,
                                dossier.forecaster.fingerprint,
                                calibration.method)
    return open_board(plan.jobs, measures_by_key, reference, capacity), dossier, checked_live


def test_end_to_end_board_matches_real_pipeline_allocation():
    board, dossier, live = _build_board()
    issued = [t for t in board.tickets if t.state == '已签发']
    assert issued, '样例班次应当产生已签发复压单'
    # 原凭证占用与真实派单的工位守恒一致
    assert board.used_slots() <= board.capacity
    view = workbench_view(board)
    assert view['冻结模型'] == dossier.forecaster.fingerprint
    assert view['已用工位'] + view['剩余工位'] == board.capacity


def test_end_to_end_supplement_lifecycle_conserves_slots():
    board, dossier, live = _build_board(capacity=6)
    occupied = next((t for t in board.tickets if t.state == '已签发'), None)
    waiting = next((t for t in board.tickets if t.state == '候补'), None)
    assert occupied is not None and waiting is not None, '容量收紧后应同时存在签发单与候补'

    # 用真实模型对一组失格量测重算风险，验证“沿用首次模型与校准”
    from beamcure.maturity_signals.service import numeric_fields
    from beamcure.strength_classifier.service import predict_one
    from beamcure.cube_validation.service import check_point
    base_vector = dossier.prepared.live
    raw_vector = next(point for point in base_vector if point.key == occupied.key)
    mild_values = (450.0, 42.0, 360.0, 33.0, 5.0, 28.0)  # 低梯度、长龄期、低水胶
    scaled, factors, excursions = numeric_fields(mild_values, dossier.feature_stamp)
    rescored_vector = replace(raw_vector, values=scaled, factors=factors,
                              excursions=excursions, input_values=mild_values)
    rescored = predict_one(dossier.forecaster, rescored_vector)
    reassessed = check_point(rescored, dossier.grading()[0])

    mark_pressed(board, occupied.key)
    before = board.used_slots()
    board = apply_supplement(
        board,
        Supplement('e2e-1', occupied.key, occupied.revision + 1, mild_values, reassessed.estimate),
    )
    # 失格则撤回并释放工位，否则照常上机；两条路径都须守恒
    if board.ticket(occupied.key).state == '已撤回':
        assert board.used_slots() <= before
        assert any(occupied.key == key for key, _ in board.vouchers[-1].revoked)
    else:
        assert board.ticket(occupied.key).state == '已上机'
        assert board.used_slots() == before

    trace = voucher_trace(board)
    assert occupied.key in trace and any(step['凭证'] == '原凭证' for step in trace[occupied.key])
    # 冻结版本在续派后保持不变
    assert all(v.reference.model_fingerprint == dossier.forecaster.fingerprint for v in board.vouchers)


def test_end_to_end_retry_does_not_duplicate_voucher():
    board, _, _ = _build_board(capacity=6)
    occupied = next(t for t in board.tickets if t.state == '已签发')
    event = Supplement('e2e-dup', occupied.key, occupied.revision + 1,
                       occupied.measures, min(0.99, occupied.estimate + 0.01))
    once = apply_supplement(board, event)
    twice = apply_supplement(once, event)
    assert twice == once
    assert len(twice.vouchers) == len(once.vouchers)

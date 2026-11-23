import pytest

import careguide.medical_triage as medical_triage
from careguide.medical_triage import evaluate_headache_triage


def test_generic_department_request_without_symptom_asks_for_symptom():
    decision = evaluate_headache_triage(
        "请先判断是否紧急，再告诉我挂什么科", history=[])

    assert decision.status == "needs_clarification"
    assert any("主要症状" in question for question in decision.questions)
    assert decision.department is None
    assert decision.run_rag is False


def test_followup_uses_recent_user_headache_context():
    decision = evaluate_headache_triage(
        "请先判断是否紧急，再告诉我挂什么科",
        history=[{"role": "user", "content": "我头有点疼"}],
    )

    assert decision.symptoms == ("headache",)
    assert decision.status == "needs_clarification"
    assert all("主要症状" not in question for question in decision.questions)
    assert decision.run_rag is False


def test_coordinated_negations_are_not_positive_red_flags():
    decision = evaluate_headache_triage(
        "今天上午头痛4分，没有发烧、呕吐、肢体无力，也没有受伤",
        history=[],
    )

    assert "focal_neurologic_deficit" not in decision.red_flags
    assert "head_injury" not in decision.red_flags
    assert "肢体无力" in decision.negated
    assert decision.status == "non_emergency_information"
    assert decision.run_rag is True


def test_positive_focal_deficit_is_not_hidden_by_unrelated_negation():
    decision = evaluate_headache_triage(
        "今天突然头痛，没有发烧，但右侧肢体无力，说话困难",
        history=[],
    )

    assert "focal_neurologic_deficit" in decision.red_flags
    assert decision.status == "emergency"
    assert decision.department == "急诊"
    assert decision.run_rag is False


def test_thunderclap_headache_preempts_retrieval():
    decision = evaluate_headache_triage(
        "突然出现一生中最严重的头痛，几秒钟达到高峰",
        history=[],
    )

    assert decision.status == "emergency"
    assert decision.run_rag is False
    assert decision.department == "急诊"
    assert "thunderclap" in decision.red_flags


def test_incomplete_headache_asks_for_onset_severity_and_red_flags():
    decision = evaluate_headache_triage("我头有点疼", history=[])

    assert decision.status == "needs_clarification"
    assert decision.run_rag is False
    assert any("什么时候" in question for question in decision.questions)
    assert any("0到10" in question for question in decision.questions)
    assert any("肢体无力" in question for question in decision.questions)


def test_current_inability_to_walk_enters_mobility_triage():
    message = "我不能走路，想去泰州市医院检查腿部，请你帮我安排"

    assert hasattr(medical_triage, 'is_mobility_triage_request')
    assert hasattr(medical_triage, 'evaluate_mobility_triage')
    assert medical_triage.is_mobility_triage_request(message, history=[]) is True
    decision = medical_triage.evaluate_mobility_triage(message, history=[])

    assert decision.status == "needs_clarification"
    assert decision.symptoms == ("mobility_impairment",)
    assert decision.department is None
    assert decision.run_rag is False
    assert any("突然" in question and "长期" in question for question in decision.questions)
    assert any("外伤" in question and "麻木" in question for question in decision.questions)


def test_sudden_inability_to_walk_is_emergency():
    message = "我刚刚突然站不住，双腿无力不能走路"

    assert hasattr(medical_triage, 'evaluate_mobility_triage')
    decision = medical_triage.evaluate_mobility_triage(message, history=[])

    assert decision.status == "emergency"
    assert decision.department == "急诊"
    assert "acute_mobility_loss" in decision.red_flags
    assert decision.run_rag is False


def test_past_unrelated_symptom_does_not_hide_current_acute_mobility_loss():
    decision = medical_triage.evaluate_medical_triage(
        "我以前腰痛，今天突然不能走路", history=[])

    assert decision is not None
    assert decision.status == "emergency"
    assert "acute_mobility_loss" in decision.red_flags


def test_negated_spinal_red_flags_are_not_treated_as_positive():
    decision = medical_triage.evaluate_mobility_triage(
        "我现在不能走路，没有大小便失禁，也没有会阴麻木", history=[])

    assert decision.status == "needs_clarification"
    assert "possible_spinal_emergency" not in decision.red_flags
    assert "大小便失禁" in decision.negated
    assert "会阴麻木" in decision.negated


def test_negated_trauma_and_weight_bearing_are_not_emergency():
    decision = medical_triage.evaluate_mobility_triage(
        "我现在不能走路，没有摔倒或外伤，也不是不能承重", history=[])

    assert decision.status == "needs_clarification"
    assert "trauma_with_inability_to_bear_weight" not in decision.red_flags


@pytest.mark.parametrize("message", [
    "我长期不能走路，是轮椅用户，想查医院无障碍入口",
    "我不是不能走路，只是想查询医院无障碍入口",
    "我担心以后不能走路，想了解康复知识",
])
def test_non_current_mobility_information_requests_stay_outside_triage(message):
    assert medical_triage.is_mobility_triage_request(message, history=[]) is False
    assert medical_triage.evaluate_medical_triage(message, history=[]) is None


def test_vague_followup_uses_recent_active_mobility_event():
    decision = medical_triage.evaluate_medical_triage(
        "现在该怎么办？",
        history=[{"role": "user", "content": "我刚刚突然不能走路"}],
    )

    assert decision is not None
    assert decision.status == "emergency"
    assert "acute_mobility_loss" in decision.red_flags


def test_unrelated_historical_sudden_word_does_not_make_current_case_emergency():
    decision = medical_triage.evaluate_medical_triage(
        "我现在不能走路",
        history=[{"role": "user", "content": "我以前突然头晕，现在早好了"}],
    )

    assert decision is not None
    assert decision.status == "needs_clarification"
    assert "acute_mobility_loss" not in decision.red_flags


def test_current_acute_event_wins_over_past_mobility_event():
    decision = medical_triage.evaluate_medical_triage(
        "我以前不能走路，现在突然不能走路", history=[])

    assert decision is not None
    assert decision.status == "emergency"
    assert "acute_mobility_loss" in decision.red_flags


def test_resolved_past_mobility_event_does_not_trigger_triage():
    decision = medical_triage.evaluate_medical_triage(
        "我以前不能走路，现在已经恢复，想查医院入口", history=[])

    assert decision is None


def test_new_acute_change_is_not_hidden_by_chronic_accessibility_request():
    decision = medical_triage.evaluate_medical_triage(
        "我长期坐轮椅，今天突然站不住，想查医院无障碍入口", history=[])

    assert decision is not None
    assert decision.status == "emergency"
    assert "acute_mobility_loss" in decision.red_flags


def test_past_trauma_is_not_joined_to_current_mobility_event():
    decision = medical_triage.evaluate_medical_triage(
        "我以前车祸后不能承重，现在只是腿麻木", history=[])

    assert decision is not None
    assert decision.status == "needs_clarification"
    assert "trauma_with_inability_to_bear_weight" not in decision.red_flags


def test_current_spinal_red_flag_before_mobility_clause_is_detected():
    decision = medical_triage.evaluate_medical_triage(
        "我大小便失禁，现在不能走路", history=[])

    assert decision is not None
    assert decision.status == "emergency"
    assert "possible_spinal_emergency" in decision.red_flags


def test_current_trauma_before_mobility_clause_is_detected():
    decision = medical_triage.evaluate_medical_triage(
        "我摔倒后无法承重，现在站不住", history=[])

    assert decision is not None
    assert decision.status == "emergency"
    assert "trauma_with_inability_to_bear_weight" in decision.red_flags


def test_unrelated_recovered_symptom_does_not_hide_current_mobility_loss():
    decision = medical_triage.evaluate_medical_triage(
        "我现在不能走路但意识已经恢复", history=[])

    assert decision is not None
    assert decision.status == "needs_clarification"


def test_coordinated_mobility_red_flag_negation_has_no_length_cutoff():
    decision = medical_triage.evaluate_medical_triage(
        "我现在不能走路，没有大小便失禁、排尿困难和会阴麻木", history=[])

    assert decision.status == "needs_clarification"
    assert "possible_spinal_emergency" not in decision.red_flags
    assert {"大小便失禁", "排尿困难", "会阴麻木"}.issubset(decision.negated)


@pytest.mark.parametrize("message", [
    "家人车祸后无法承重，我现在腿麻木",
    "家人大小便失禁，我现在不能走路",
])
def test_red_flags_from_another_person_are_not_merged(message):
    decision = medical_triage.evaluate_medical_triage(message, history=[])

    assert decision is not None
    assert decision.status == "needs_clarification"
    assert decision.red_flags == ()


def test_positive_red_flag_after_contrast_ends_negation_scope():
    decision = medical_triage.evaluate_medical_triage(
        "我现在不能走路，没有大小便失禁却有会阴麻木", history=[])

    assert decision.status == "emergency"
    assert "possible_spinal_emergency" in decision.red_flags
    assert "会阴麻木" not in decision.negated


@pytest.mark.parametrize("message", [
    "我想了解腿麻木的常见原因",
    "我看到新闻里患者突然不能走路，这是怎么回事",
])
def test_educational_or_news_mobility_text_does_not_enter_live_triage(message):
    assert medical_triage.evaluate_medical_triage(message, history=[]) is None


@pytest.mark.parametrize("message, expected_status", [
    ("我突然双腿瘫痪，完全不能动", "emergency"),
    ("患者今天下肢无力，站不起来", "needs_clarification"),
    ("我刚刚双腿没力气，走不了", "emergency"),
])
def test_common_severe_lower_limb_expressions_enter_triage(message, expected_status):
    decision = medical_triage.evaluate_medical_triage(message, history=[])

    assert decision is not None
    assert decision.status == expected_status


@pytest.mark.parametrize("message", [
    "我想了解一下，今天突然不能走路该怎么办",
    "家人想了解，患者现在突然不能走路该怎么办",
])
def test_information_word_does_not_hide_explicit_current_event(message):
    decision = medical_triage.evaluate_medical_triage(message, history=[])

    assert decision is not None
    assert decision.status == "emergency"

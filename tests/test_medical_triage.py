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

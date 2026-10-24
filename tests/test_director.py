from datetime import timedelta

from app.interview import director
from app.interview.director import Limits

from .factories import make_concept, make_doc, make_eval, make_session

LIMITS = Limits(interview_minutes=15, max_concepts=6, max_answers_per_concept=4, max_hints_per_concept=1)


def setup_two_concepts():
    doc = make_doc(make_concept("c1"), make_concept("c2"))
    return doc, make_session(doc)


def record(session, evaluation, hint_given=False):
    director.update_progress(session.progress[session.current_concept_id], evaluation, hint_given)


def test_plan_puts_prerequisites_first_and_keeps_most_important():
    doc = make_doc(
        make_concept("c1", importance=1),
        make_concept("c2", importance=3, prerequisites=["c3"]),
        make_concept("c3", importance=2),
    )
    assert director.plan_concept_order(doc.knowledge_map, limit=3) == ["c1", "c3", "c2"]
    assert director.plan_concept_order(doc.knowledge_map, limit=2) == ["c3", "c2"]


def test_strong_answer_moves_to_next_concept():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=4, completeness=4, reasoning=4, bloom_level=3)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "next_concept"
    assert decision.concept_id == "c2"
    assert decision.question == "c2 là gì?"


def test_correct_but_shallow_answer_is_probed_with_higher_bloom_question():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=3, completeness=3, reasoning=2, bloom_level=2)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "probe_deeper"
    assert decision.question == "Áp dụng c1 thế nào?"


def test_incomplete_answer_is_probed_with_evaluator_follow_up():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=3, completeness=1, bloom_level=2)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "probe_deeper"
    assert decision.question == evaluation.suggested_follow_up


def test_dont_know_gets_one_hint_then_moves_on():
    doc, session = setup_two_concepts()
    evaluation = make_eval(intent="dont_know", correctness=0, completeness=0, reasoning=0, bloom_level=0)
    record(session, evaluation)
    first = director.decide(session, doc, evaluation, LIMITS)
    assert first.action == "hint"
    director.apply_directive(session, first)

    record(session, evaluation, hint_given=True)
    second = director.decide(session, doc, evaluation, LIMITS)
    assert second.action == "next_concept"
    director.apply_directive(session, second)
    assert session.progress["c1"].status == "gap"
    assert session.current_concept_id == "c2"


def test_misconception_triggers_challenge():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=2, misconceptions=["Nhầm A với B"])
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "challenge"
    assert "Nhầm A với B" in decision.note


def test_confident_wrong_belief_is_challenged_not_hinted():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=0, completeness=0, confidence_signal="high", misconceptions=["Mỗi cổng chỉ có một socket"])
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "challenge"


def test_hesitant_wrong_guess_gets_a_hint():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=0, completeness=0, confidence_signal="low", misconceptions=["Đoán router đánh dấu gói"])
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "hint"


def test_wrong_answer_without_misconception_gets_a_hint():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=1, completeness=0, confidence_signal="high")
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "hint"


def test_clarification_request_does_not_count_as_evidence():
    doc, session = setup_two_concepts()
    session.progress["c1"].asked_questions.append("c1 là gì?")
    session.last_question = "Mình hiểu rồi, cảm ơn bạn. Giờ sang chủ đề mới nhé: c1 là gì?"
    evaluation = make_eval(intent="clarification_request", correctness=0, completeness=0, reasoning=0)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "clarify"
    assert decision.question == "c1 là gì?"
    assert session.progress["c1"].evidence_count == 0
    assert session.progress["c1"].attempts == 1


def test_time_up_wraps_up():
    doc, session = setup_two_concepts()
    evaluation = make_eval()
    later = session.created_at + timedelta(minutes=16)
    assert director.decide(session, doc, evaluation, LIMITS, now=later).action == "wrap_up"


def test_last_concept_mastered_wraps_up():
    doc = make_doc(make_concept("c1"))
    session = make_session(doc)
    evaluation = make_eval(correctness=4, completeness=4, reasoning=4, bloom_level=4)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "wrap_up"
    director.apply_directive(session, decision)
    assert session.wrap_up_requested
    assert session.progress["c1"].status == "mastered"


def test_answer_budget_closes_concept():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=2, completeness=2, reasoning=2, bloom_level=2)
    for _ in range(LIMITS.max_answers_per_concept):
        record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    assert decision.action == "next_concept"


def test_deferred_switch_keeps_current_concept_until_voice_agent_moves_on():
    doc, session = setup_two_concepts()
    evaluation = make_eval(correctness=4, completeness=4, reasoning=4, bloom_level=3)
    record(session, evaluation)
    decision = director.decide(session, doc, evaluation, LIMITS)
    director.apply_directive(session, decision, defer_switch=True)
    assert session.current_concept_id == "c1"
    assert session.pending_concept_id == "c2"
    assert session.progress["c1"].status == "mastered"


def test_hint_penalty_lowers_score():
    _, session = setup_two_concepts()
    evaluation = make_eval(correctness=4, completeness=4, reasoning=4, bloom_level=3)
    director.update_progress(session.progress["c1"], evaluation, hint_given=False)
    director.update_progress(session.progress["c2"], evaluation, hint_given=True)
    assert session.progress["c2"].score < session.progress["c1"].score

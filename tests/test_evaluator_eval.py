"""Kiểm tra bộ dữ liệu và phần tính chỉ số của evals/evaluator_eval.py, không gọi Gemini."""

import asyncio
from collections import Counter
from datetime import datetime

import pytest

import evals.evaluator_eval as ev

from .factories import make_eval

# Đánh giá giả cho từng nhãn: nếu director nhận đúng đánh giá này thì phải ra đúng hành động đó.
FAKE_EVALUATION = {
    "next_concept": dict(correctness=4, completeness=4, reasoning=4, bloom_level=3),
    "probe_deeper": dict(correctness=3, completeness=2, bloom_level=2),
    "challenge": dict(correctness=2, completeness=2, misconceptions=["Hiểu nhầm"]),
    "hint": dict(intent="dont_know", correctness=0, completeness=0, reasoning=0, bloom_level=0),
    "clarify": dict(intent="clarification_request"),
    "encourage": dict(intent="thinking_aloud"),
    "redirect": dict(intent="off_topic"),
}


def test_cases_are_well_formed():
    doc, cases = ev.load_cases()
    concept_ids = {c.id for c in doc.knowledge_map.concepts}
    chunk_ids = {c.id for c in doc.chunks}

    assert len({c["id"] for c in cases}) == len(cases)
    for concept in doc.knowledge_map.concepts:
        assert set(concept.source_chunks) <= chunk_ids
    for case in cases:
        assert case["concept_id"] in concept_ids, case["id"]
        assert case["expected_action"] in ev.ACTIONS, case["id"]
        assert case["answer"].strip() and case["question"].strip()
    counts = Counter(c["expected_action"] for c in cases)
    assert all(counts[action] >= 2 for action in ev.ACTIONS), counts


def test_every_label_is_reachable_through_the_real_director(monkeypatch):
    doc, cases = ev.load_cases()

    async def fake_evaluate(doc, concept, question, answer, history):
        case = next(c for c in cases if c["answer"] == answer)
        return make_eval(**FAKE_EVALUATION[case["expected_action"]])

    monkeypatch.setattr(ev, "evaluate_answer", fake_evaluate)
    results = asyncio.run(ev.run_all(doc, cases, concurrency=4))

    assert [r.id for r in results if not r.correct] == []
    assert ev.summarize(results)["accuracy"] == 1.0


def test_failed_calls_are_reported_but_not_scored(monkeypatch):
    doc, cases = ev.load_cases()

    async def failing_evaluate(*args, **kwargs):
        raise RuntimeError("429 hết quota")

    monkeypatch.setattr(ev, "evaluate_answer", failing_evaluate)
    results = asyncio.run(ev.run_all(doc, cases[:3], concurrency=1))
    summary = ev.summarize(results)

    assert summary["errors"] == 3
    assert "429 hết quota" in ev.render_report(summary, results, "gemini-test", datetime(2026, 10, 24))


def test_cohen_kappa():
    assert ev.cohen_kappa([("a", "a"), ("b", "b")]) == 1.0
    # Đoán toàn một nhãn: khớp 50% nhưng không hơn ngẫu nhiên.
    assert ev.cohen_kappa([("a", "a"), ("b", "a")]) == 0.0
    assert ev.cohen_kappa([("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")]) == pytest.approx(0.5)


def test_report_shows_metrics_and_misses():
    results = [
        ev.CaseResult("x1", "c1", "hint", "hint", False, "low", 0, 0, 0, [], "Không biết"),
        ev.CaseResult("x2", "c1", "probe_deeper", "next_concept", True, "low", 4, 4, 3, [], "Đọc thuộc định nghĩa"),
    ]
    report = ev.render_report(ev.summarize(results), results, "gemini-test", datetime(2026, 10, 24, 9, 30))

    assert "**50%**" in report
    assert "`x2` | Hỏi sâu | Chuyển chủ đề | Đọc thuộc định nghĩa" in report
    assert "0% trên 1 ca" in report

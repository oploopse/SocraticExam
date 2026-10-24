"""Luật quyết định hỏi sâu, gợi ý hay chuyển chủ đề.

Phần này cố ý không gọi LLM: evaluator cung cấp dữ liệu, còn quyết định là code thuần để dễ đoán và dễ test.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import settings
from ..schemas import (
    Concept,
    ConceptProgress,
    ConceptStatus,
    Directive,
    DocumentRecord,
    KnowledgeMap,
    SessionRecord,
    TurnEvaluation,
    utcnow,
)

TARGET_BLOOM = 3
MASTERED_SCORE = 0.75
PARTIAL_SCORE = 0.45
HINT_PENALTY = 0.85
NO_EVIDENCE_INTENTS = {"clarification_request", "thinking_aloud", "off_topic", "small_talk"}


@dataclass(frozen=True)
class Limits:
    interview_minutes: int
    max_concepts: int
    max_answers_per_concept: int
    max_hints_per_concept: int
    # Tổng số câu trả lời cần có trong buổi, tính cả câu hỏi đào sâu; None là không giới hạn.
    question_limit: int | None = None

    @classmethod
    def from_settings(cls) -> "Limits":
        return cls(
            interview_minutes=settings.interview_minutes,
            max_concepts=settings.max_concepts_per_session,
            max_answers_per_concept=settings.max_answers_per_concept,
            max_hints_per_concept=settings.max_hints_per_concept,
        )

    @classmethod
    def for_session(cls, session: SessionRecord) -> "Limits":
        return cls(
            interview_minutes=time_limit_minutes(session),
            max_concepts=settings.max_concepts_per_session,
            max_answers_per_concept=settings.max_answers_per_concept,
            max_hints_per_concept=settings.max_hints_per_concept,
            question_limit=session.question_limit,
        )


def time_limit_minutes(session: SessionRecord) -> int:
    return session.time_limit_minutes or settings.interview_minutes


def concept_pages(doc: DocumentRecord, concept: Concept) -> set[int]:
    chunks = doc.chunks_by_id()
    return {chunks[c].page for c in concept.source_chunks if c in chunks and chunks[c].page}


def concepts_in_pages(doc: DocumentRecord, page_from: int | None, page_to: int | None) -> list[Concept]:
    """Các concept có nội dung nằm trong phạm vi trang. Tài liệu không có số trang thì giữ nguyên."""
    concepts = doc.knowledge_map.concepts
    if page_from is None and page_to is None:
        return list(concepts)
    low, high = page_from or 1, page_to or 10**9
    kept = []
    for concept in concepts:
        pages = concept_pages(doc, concept)
        if not pages or any(low <= page <= high for page in pages):
            kept.append(concept)
    return kept


def answered_count(session: SessionRecord) -> int:
    return sum(1 for r in session.evaluations if r.evaluation.intent not in NO_EVIDENCE_INTENTS)


def plan_concept_order(knowledge_map: KnowledgeMap, limit: int) -> list[str]:
    """Chọn các concept quan trọng nhất, rồi sắp theo thứ tự tài liệu sao cho concept tiên quyết đứng trước."""
    index = {c.id: i for i, c in enumerate(knowledge_map.concepts)}
    chosen = sorted(knowledge_map.concepts, key=lambda c: (-c.importance, index[c.id]))[:limit]
    chosen_ids = {c.id for c in chosen}
    remaining = sorted(chosen, key=lambda c: index[c.id])
    order: list[str] = []
    while remaining:
        ready = next(
            (c for c in remaining if all(p in order or p not in chosen_ids for p in c.prerequisites)),
            remaining[0],
        )
        order.append(ready.id)
        remaining.remove(ready)
    return order


def answer_score(evaluation: TurnEvaluation) -> float:
    if evaluation.intent == "dont_know":
        return 0.0
    bloom = min(max(evaluation.bloom_level, 0), TARGET_BLOOM)
    return (
        0.40 * evaluation.correctness / 4
        + 0.25 * evaluation.completeness / 4
        + 0.20 * evaluation.reasoning / 4
        + 0.15 * bloom / TARGET_BLOOM
    )


def update_progress(progress: ConceptProgress, evaluation: TurnEvaluation, hint_given: bool) -> None:
    progress.attempts += 1
    if evaluation.intent in NO_EVIDENCE_INTENTS:
        return
    score = answer_score(evaluation) * (HINT_PENALTY if hint_given else 1.0)
    progress.score = (progress.score * progress.evidence_count + score) / (progress.evidence_count + 1)
    progress.evidence_count += 1
    if evaluation.correctness >= 2:
        progress.max_bloom = max(progress.max_bloom, evaluation.bloom_level)
    progress.covered_key_points = list(dict.fromkeys([*progress.covered_key_points, *evaluation.covered_key_points]))
    progress.misconceptions = list(dict.fromkeys([*progress.misconceptions, *evaluation.misconceptions]))
    if evaluation.rote_signal == "high":
        progress.rote_flags += 1


def final_status(progress: ConceptProgress) -> ConceptStatus:
    if progress.evidence_count == 0:
        return "unassessed"
    if progress.score >= MASTERED_SCORE and progress.max_bloom >= TARGET_BLOOM:
        return "mastered"
    if progress.score >= PARTIAL_SCORE:
        return "partial"
    return "gap"


def time_is_up(session: SessionRecord, limits: Limits, now: datetime | None = None) -> bool:
    return (now or utcnow()) - session.created_at >= timedelta(minutes=limits.interview_minutes)


def next_concept_id(session: SessionRecord, limits: Limits) -> str | None:
    started = sum(1 for p in session.progress.values() if p.status != "pending")
    if started >= limits.max_concepts:
        return None
    return next((cid for cid in session.concept_order if session.progress[cid].status == "pending"), None)


def first_question(concept: Concept) -> str:
    return concept.questions[0].text


def current_question(session: SessionRecord) -> str:
    """Câu hỏi đã soạn gần nhất của chủ đề hiện tại, không kèm lời dẫn của người phỏng vấn."""
    asked = session.progress[session.current_concept_id].asked_questions
    return asked[-1] if asked else session.last_question


def pick_probe(concept: Concept, progress: ConceptProgress, evaluation: TurnEvaluation) -> str:
    planned = [
        q
        for q in concept.questions
        if q.bloom_level > progress.max_bloom and q.text not in progress.asked_questions
    ]
    # Đủ ý nhưng còn nông: nâng lên câu hỏi mức Bloom cao hơn đã soạn sẵn từ tài liệu.
    if evaluation.completeness >= 3 and planned:
        return planned[0].text
    if evaluation.suggested_follow_up.strip():
        return evaluation.suggested_follow_up.strip()
    return planned[0].text if planned else concept.questions[-1].text


def _advance(
    session: SessionRecord, doc: DocumentRecord, limits: Limits, reason: str, force_wrap_up: bool = False
) -> Directive:
    next_id = None if force_wrap_up else next_concept_id(session, limits)
    if next_id is None:
        return Directive(action="wrap_up", reason=reason if force_wrap_up else f"{reason}; đã hết chủ đề cần hỏi")
    concept = doc.concept(next_id)
    return Directive(
        action="next_concept",
        concept_id=next_id,
        question=first_question(concept),
        note="Ghi nhận ngắn câu trả lời vừa rồi rồi chuyển ý một cách tự nhiên.",
        reason=reason,
    )


def decide(
    session: SessionRecord,
    doc: DocumentRecord,
    evaluation: TurnEvaluation,
    limits: Limits,
    now: datetime | None = None,
) -> Directive:
    concept_id = session.current_concept_id
    concept = doc.concept(concept_id)
    progress = session.progress[concept_id]

    if time_is_up(session, limits, now):
        return _advance(session, doc, limits, "Hết thời gian phỏng vấn", force_wrap_up=True)
    if (
        limits.question_limit
        and evaluation.intent not in NO_EVIDENCE_INTENTS
        and answered_count(session) + 1 >= limits.question_limit
    ):
        return _advance(session, doc, limits, "Đã hỏi đủ số câu đã chọn", force_wrap_up=True)

    question = current_question(session)
    if evaluation.intent == "clarification_request":
        return Directive(
            action="clarify", concept_id=concept_id, question=question,
            reason="Người học chưa hiểu câu hỏi",
        )
    if evaluation.intent == "thinking_aloud":
        return Directive(
            action="encourage", concept_id=concept_id, question=question,
            reason="Người học đang suy nghĩ",
        )
    if evaluation.intent in {"off_topic", "small_talk"}:
        return Directive(
            action="redirect", concept_id=concept_id, question=question,
            reason="Câu trả lời chưa đi vào câu hỏi",
        )

    if progress.attempts >= limits.max_answers_per_concept + 2:
        return _advance(session, doc, limits, "Đã dùng hết số lượt cho chủ đề này")

    def hint_or_advance() -> Directive:
        if progress.hints < limits.max_hints_per_concept:
            return Directive(
                action="hint", concept_id=concept_id, question=question,
                note="Gợi ý một hướng suy nghĩ, tuyệt đối không nêu đáp án.",
                reason="Người học chưa trả lời được",
            )
        return _advance(session, doc, limits, "Đã gợi ý nhưng người học vẫn chưa trả lời được")

    if evaluation.intent == "dont_know":
        return hint_or_advance()

    # Sai vì hiểu nhầm khác sai vì chưa biết: người học nói chắc chắn một điều sai thì cần phản ví dụ để
    # tự thấy mâu thuẫn; còn đoán mò ("chắc là...") thì gợi ý một hướng suy nghĩ có ích hơn.
    # Đo bằng evals/evaluator_eval.py: trước khi tách hai trường hợp, mọi câu hiểu lầm có correctness <= 1
    # đều thành gợi ý.
    holds_misconception = bool(evaluation.misconceptions) and (
        evaluation.confidence_signal != "low" or evaluation.correctness >= 2
    )
    if holds_misconception and progress.evidence_count < limits.max_answers_per_concept:
        return Directive(
            action="challenge",
            concept_id=concept_id,
            question=evaluation.suggested_follow_up.strip() or pick_probe(concept, progress, evaluation),
            note="Người học có thể đang hiểu nhầm: " + "; ".join(evaluation.misconceptions),
            reason="Phát hiện hiểu lầm",
        )

    if evaluation.correctness <= 1:
        return hint_or_advance()

    strong_now = (
        evaluation.correctness >= 3
        and evaluation.completeness >= 3
        and evaluation.bloom_level >= TARGET_BLOOM
    )
    consistently_strong = (
        progress.evidence_count >= 2
        and progress.score >= MASTERED_SCORE
        and progress.max_bloom >= TARGET_BLOOM
    )
    if strong_now or consistently_strong:
        return _advance(session, doc, limits, "Đã đủ bằng chứng hiểu tốt")

    if progress.evidence_count >= limits.max_answers_per_concept:
        return _advance(session, doc, limits, "Đã hỏi đủ số lượt cho chủ đề này")

    reason = (
        "Câu trả lời có dấu hiệu học thuộc"
        if evaluation.rote_signal == "high"
        else "Cần thêm bằng chứng ở mức hiểu sâu hơn"
    )
    return Directive(
        action="probe_deeper", concept_id=concept_id,
        question=pick_probe(concept, progress, evaluation), reason=reason,
    )


def switch_concept(session: SessionRecord, concept_id: str) -> None:
    session.current_concept_id = concept_id
    session.progress[concept_id].status = "in_progress"


def apply_directive(session: SessionRecord, directive: Directive, defer_switch: bool = False) -> None:
    """Cập nhật trạng thái phiên theo quyết định.

    defer_switch dùng cho voice mode: voice agent chỉ chuyển chủ đề ở lượt nói kế tiếp,
    nên câu trả lời đến trước đó vẫn thuộc về chủ đề cũ.
    """
    progress = session.progress[session.current_concept_id]
    if directive.action == "hint":
        progress.hints += 1
    elif directive.action in {"probe_deeper", "challenge"}:
        progress.probes += 1
        if directive.question:
            progress.asked_questions.append(directive.question)

    if directive.action in {"next_concept", "wrap_up"}:
        progress.status = final_status(progress)

    if directive.action == "next_concept":
        if directive.question:
            session.progress[directive.concept_id].asked_questions.append(directive.question)
        if defer_switch:
            session.pending_concept_id = directive.concept_id
        else:
            switch_concept(session, directive.concept_id)
    elif directive.action == "wrap_up":
        session.wrap_up_requested = True
    session.last_action = directive.action

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tài liệu và bản đồ kiến thức
# ---------------------------------------------------------------------------


class Chunk(BaseModel):
    id: str
    page: int | None = None
    text: str


class KeyPoint(BaseModel):
    id: str = Field(description='Mã ý chính, dạng "c1.k1"')
    text: str = Field(description="Mệnh đề ngắn, kiểm chứng được từ tài liệu")


class Question(BaseModel):
    bloom_level: int = Field(
        description="Mức độ theo thang Bloom: 1 nhớ, 2 hiểu, 3 vận dụng, 4 phân tích, 5 đánh giá, 6 sáng tạo"
    )
    text: str = Field(description="Câu hỏi ngắn, tự nhiên khi nói thành lời")


class Concept(BaseModel):
    id: str = Field(description='Mã concept, dạng "c1"')
    name: str
    summary: str = Field(description="Tóm tắt concept trong 1-2 câu")
    importance: int = Field(description="3 = cốt lõi, 2 = quan trọng, 1 = bổ trợ")
    prerequisites: list[str] = Field(description="Mã các concept cần hiểu trước concept này")
    source_chunks: list[str] = Field(description='Mã các đoạn tài liệu chứa nội dung concept, dạng "ch12"')
    key_points: list[KeyPoint] = Field(description="2-5 ý chính người học cần nêu được")
    misconceptions: list[str] = Field(description="1-3 hiểu lầm hay gặp")
    questions: list[Question] = Field(description="3-4 câu hỏi ở các mức Bloom khác nhau")


class KnowledgeMap(BaseModel):
    title: str = Field(description="Tên ngắn gọn của tài liệu")
    summary: str = Field(description="Tóm tắt tài liệu trong 2-3 câu")
    concepts: list[Concept]


class DocumentRecord(BaseModel):
    id: str
    filename: str
    created_at: datetime = Field(default_factory=utcnow)
    char_count: int
    truncated: bool = False
    # SHA-256 của file gốc, để tải lại cùng file thì dùng lại bản đồ kiến thức thay vì gọi Gemini.
    content_hash: str | None = None
    chunks: list[Chunk]
    knowledge_map: KnowledgeMap

    def concept(self, concept_id: str | None) -> Concept | None:
        return next((c for c in self.knowledge_map.concepts if c.id == concept_id), None)

    def chunks_by_id(self) -> dict[str, Chunk]:
        return {c.id: c for c in self.chunks}


# ---------------------------------------------------------------------------
# Đánh giá từng lượt và quyết định của director
# ---------------------------------------------------------------------------

Intent = Literal[
    "answer",
    "partial_answer",
    "dont_know",
    "clarification_request",
    "thinking_aloud",
    "off_topic",
    "small_talk",
]
Signal = Literal["low", "medium", "high"]


class TurnEvaluation(BaseModel):
    intent: Intent
    correctness: int = Field(description="0-4")
    completeness: int = Field(description="0-4")
    reasoning: int = Field(description="0-4")
    bloom_level: int = Field(description="0-6, mức cao nhất thể hiện được; 0 nếu không có bằng chứng")
    covered_key_points: list[str] = Field(description="Mã các ý chính người học đã nêu được")
    missing_key_points: list[str] = Field(description="Mã các ý chính liên quan câu hỏi nhưng chưa nêu")
    misconceptions: list[str] = Field(description="Các hiểu lầm thể hiện trong câu trả lời")
    rote_signal: Signal = Field(description="Mức độ có dấu hiệu học thuộc mà không hiểu")
    confidence_signal: Signal = Field(description="Mức độ tự tin thể hiện qua cách diễn đạt")
    evidence_quote: str = Field(description="Trích nguyên văn ngắn từ câu trả lời; rỗng nếu không có nội dung")
    summary: str = Field(description="Một câu nhận xét ngắn cho giáo viên")
    suggested_follow_up: str = Field(description="Một câu hỏi để hỏi sâu hơn, không lộ đáp án")


Action = Literal[
    "ask_main",
    "probe_deeper",
    "challenge",
    "hint",
    "clarify",
    "encourage",
    "redirect",
    "next_concept",
    "wrap_up",
]


class Directive(BaseModel):
    action: Action
    concept_id: str | None = None
    question: str | None = None
    note: str = ""
    reason: str = ""


ConceptStatus = Literal["pending", "in_progress", "mastered", "partial", "gap", "unassessed"]


class ConceptProgress(BaseModel):
    concept_id: str
    status: ConceptStatus = "pending"
    score: float = 0.0
    evidence_count: int = 0
    attempts: int = 0
    hints: int = 0
    probes: int = 0
    max_bloom: int = 0
    rote_flags: int = 0
    covered_key_points: list[str] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    asked_questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Báo cáo
# ---------------------------------------------------------------------------

Level = Literal["strong", "good", "basic", "gap", "not_assessed"]


class ConceptFeedback(BaseModel):
    concept_id: str
    strengths: list[str] = Field(description="Điểm mạnh, mỗi ý một câu ngắn")
    gaps: list[str] = Field(description="Lỗ hổng kiến thức, mỗi ý một câu ngắn")
    advice: str = Field(description="Lời khuyên ôn tập cụ thể cho concept này")


class ReportNarrative(BaseModel):
    summary_for_learner: str
    summary_for_teacher: str
    concept_feedback: list[ConceptFeedback]
    study_plan: list[str]
    teacher_notes: list[str]


class ConceptReport(BaseModel):
    concept_id: str
    name: str
    importance: int
    status: ConceptStatus
    level: Level
    score: int
    max_bloom: int
    hints: int
    strengths: list[str]
    gaps: list[str]
    misconceptions: list[str]
    advice: str
    evidence: list[str]
    review_pages: list[int]


class Report(BaseModel):
    generated_at: datetime = Field(default_factory=utcnow)
    overall_score: int
    overall_level: Level
    summary_for_learner: str
    summary_for_teacher: str
    concepts: list[ConceptReport]
    study_plan: list[str]
    teacher_notes: list[str]


# ---------------------------------------------------------------------------
# Phiên phỏng vấn
# ---------------------------------------------------------------------------


class Turn(BaseModel):
    role: Literal["interviewer", "learner"]
    text: str
    concept_id: str | None = None
    at: datetime = Field(default_factory=utcnow)


class EvaluationRecord(BaseModel):
    concept_id: str
    question: str
    answer: str
    evaluation: TurnEvaluation
    directive: Directive | None = None
    at: datetime = Field(default_factory=utcnow)


class SessionRecord(BaseModel):
    id: str
    document_id: str
    learner_name: str
    mode: Literal["text", "voice"]
    status: Literal["active", "finished"] = "active"
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    concept_order: list[str]
    # Tuỳ chọn ở màn Thiết lập; None nghĩa là dùng mặc định trong .env (phiên tạo trước khi có các trường này).
    time_limit_minutes: int | None = None
    question_limit: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    current_concept_id: str | None = None
    pending_concept_id: str | None = None
    wrap_up_requested: bool = False
    last_action: Action | None = None
    last_question: str = ""
    progress: dict[str, ConceptProgress]
    turns: list[Turn] = Field(default_factory=list)
    evaluations: list[EvaluationRecord] = Field(default_factory=list)
    report: Report | None = None

import pytest
from fastapi.testclient import TestClient

import app.interview.evaluator as evaluator_module
import app.interview.interviewer as interviewer_module
import app.interview.report as report_module
import app.knowledge as knowledge_module
import app.main as main
from app.ratelimit import limiter
from app.schemas import ConceptFeedback, KnowledgeMap, ReportNarrative, TurnEvaluation
from app.storage import JsonStore

from .factories import make_concept, make_eval


async def fake_generate_json(model, system, prompt, schema, thinking_level=None, timeout_s=None):
    if schema is KnowledgeMap:
        return KnowledgeMap(title="Mạng máy tính", summary="Tóm tắt", concepts=[make_concept("A"), make_concept("B")])
    if schema is TurnEvaluation:
        return make_eval(correctness=4, completeness=4, reasoning=4, bloom_level=3, covered_key_points=["c1.k1"])
    if schema is ReportNarrative:
        return ReportNarrative(
            summary_for_learner="Bạn làm tốt.",
            summary_for_teacher="Hiểu thật.",
            concept_feedback=[ConceptFeedback(concept_id="c1", strengths=["Rõ ràng"], gaps=[], advice="Ôn thêm")],
            study_plan=["Ôn chương 2"],
            teacher_notes=[],
        )
    raise AssertionError(f"Schema không mong đợi: {schema}")


async def fake_generate_text(model, system, prompt, thinking_level=None):
    return "Câu nói của người phỏng vấn"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Mỗi test bắt đầu với bộ đếm lượt trống, để các test không ăn lượt của nhau."""
    limiter.clear()
    yield
    limiter.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dùng kho dữ liệu tạm và LLM giả."""
    store = JsonStore(tmp_path)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.engine, "store", store)
    monkeypatch.setattr(main.settings, "app_access_token", "")
    for module in (knowledge_module, evaluator_module, report_module):
        monkeypatch.setattr(module, "generate_json", fake_generate_json)
    monkeypatch.setattr(interviewer_module, "generate_text", fake_generate_text)
    return TestClient(main.app)


@pytest.fixture
def document(client):
    response = client.post("/api/documents", files={"file": ("bai.txt", "Nội dung bài học.\n\nĐoạn hai.".encode())})
    assert response.status_code == 200, response.text
    return response.json()

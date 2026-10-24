"""Đo xem evaluator (Gemini chấm điểm) + director (luật quyết định) có làm đúng việc giáo viên sẽ làm không.

Mỗi ca trong evaluator_cases.json là một câu trả lời đã gắn nhãn tay hành động mong đợi ở lượt đầu
của chủ đề (chuyển chủ đề, hỏi sâu, phản biện, gợi ý...). Script cho evaluator chấm từng câu, đưa
kết quả qua đúng director.decide() mà ứng dụng dùng, rồi so với nhãn.

Chạy (cần GEMINI_API_KEY trong .env, khoảng 42 lần gọi BRAIN_MODEL):

    python -m evals.evaluator_eval
    python -m evals.evaluator_eval --concurrency 1 --limit 10

Báo cáo Markdown và kết quả thô được ghi vào evals/results/.
"""

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.interview import director
from app.interview.evaluator import evaluate_answer
from app.schemas import Chunk, ConceptProgress, DocumentRecord, KnowledgeMap, SessionRecord

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "evaluator_cases.json"
RESULTS_DIR = EVALS_DIR / "results"

ACTIONS = ["next_concept", "probe_deeper", "challenge", "hint", "clarify", "encourage", "redirect"]
ACTION_LABELS = {
    "next_concept": "Chuyển chủ đề",
    "probe_deeper": "Hỏi sâu",
    "challenge": "Phản biện",
    "hint": "Gợi ý",
    "clarify": "Diễn đạt lại",
    "encourage": "Động viên",
    "redirect": "Kéo về câu hỏi",
}
# Đủ rộng để thời gian không bao giờ là lý do quyết định trong lúc đo.
LIMITS = director.Limits(interview_minutes=600, max_concepts=6, max_answers_per_concept=4, max_hints_per_concept=1)


@dataclass
class CaseResult:
    id: str
    concept_id: str
    expected: str
    predicted: str
    rote_expected: bool
    rote_signal: str
    correctness: int
    completeness: int
    bloom_level: int
    misconceptions: list[str]
    summary: str
    error: str = ""
    confidence_signal: str = ""

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


def load_cases(path: Path = CASES_PATH) -> tuple[DocumentRecord, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    spec = data["document"]
    doc = DocumentRecord(
        id="e" * 12,
        filename="Mang_may_tinh_Chuong3.pdf",
        char_count=sum(len(c["text"]) for c in spec["chunks"]),
        chunks=[Chunk(**c) for c in spec["chunks"]],
        knowledge_map=KnowledgeMap(title=spec["title"], summary=spec["summary"], concepts=spec["concepts"]),
    )
    return doc, data["cases"]


def fresh_session(doc: DocumentRecord, concept_id: str, question: str) -> SessionRecord:
    """Phiên vừa hỏi câu đầu tiên của concept_id; các concept khác còn chờ để có chỗ chuyển sang."""
    order = [concept_id] + [c.id for c in doc.knowledge_map.concepts if c.id != concept_id]
    session = SessionRecord(
        id="f" * 12,
        document_id=doc.id,
        learner_name="Lan",
        mode="text",
        concept_order=order,
        progress={cid: ConceptProgress(concept_id=cid) for cid in order},
        last_question=question,
    )
    director.switch_concept(session, concept_id)
    session.progress[concept_id].asked_questions.append(question)
    return session


async def run_case(doc: DocumentRecord, case: dict) -> CaseResult:
    concept = doc.concept(case["concept_id"])
    base = dict(
        id=case["id"],
        concept_id=case["concept_id"],
        expected=case["expected_action"],
        rote_expected=bool(case.get("rote")),
    )
    try:
        evaluation = await evaluate_answer(doc, concept, case["question"], case["answer"], history=[])
    except Exception as exc:  # Một ca lỗi (hết quota...) không làm hỏng cả lần đo.
        return CaseResult(
            **base, predicted="error", rote_signal="", correctness=0, completeness=0, bloom_level=0,
            misconceptions=[], summary="", error=str(exc),
        )
    session = fresh_session(doc, concept.id, case["question"])
    # Cùng thứ tự với InterviewEngine: ghi nhận câu trả lời vào tiến độ rồi mới quyết định.
    director.update_progress(session.progress[concept.id], evaluation, hint_given=False)
    directive = director.decide(session, doc, evaluation, LIMITS)
    return CaseResult(
        **base,
        predicted=directive.action,
        rote_signal=evaluation.rote_signal,
        correctness=evaluation.correctness,
        completeness=evaluation.completeness,
        bloom_level=evaluation.bloom_level,
        misconceptions=evaluation.misconceptions,
        summary=evaluation.summary,
        confidence_signal=evaluation.confidence_signal,
    )


async def run_all(doc: DocumentRecord, cases: list[dict], concurrency: int) -> list[CaseResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(case: dict) -> CaseResult:
        async with semaphore:
            result = await run_case(doc, case)
            mark = "✓" if result.correct else "✗"
            print(f"{mark} {result.id}: mong đợi {result.expected}, được {result.predicted}", flush=True)
            return result

    return await asyncio.gather(*(guarded(case) for case in cases))


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """Độ đồng thuận giữa nhãn tay và máy, đã trừ phần trùng khớp do ngẫu nhiên (1 = khớp hoàn toàn)."""
    n = len(pairs)
    if n == 0:
        return 0.0
    observed = sum(1 for a, b in pairs if a == b) / n
    expected_counts = Counter(a for a, _ in pairs)
    predicted_counts = Counter(b for _, b in pairs)
    chance = sum(expected_counts[k] * predicted_counts[k] for k in expected_counts) / (n * n)
    return 1.0 if chance == 1 else (observed - chance) / (1 - chance)


def summarize(results: list[CaseResult]) -> dict:
    scored = [r for r in results if r.predicted != "error"]
    pairs = [(r.expected, r.predicted) for r in scored]
    per_action = {}
    for action in ACTIONS:
        tp = sum(1 for e, p in pairs if e == action and p == action)
        support = sum(1 for e, _ in pairs if e == action)
        predicted = sum(1 for _, p in pairs if p == action)
        per_action[action] = {
            "support": support,
            "precision": tp / predicted if predicted else None,
            "recall": tp / support if support else None,
        }
    rote_cases = [r for r in scored if r.rote_expected]
    return {
        "total": len(results),
        "errors": len(results) - len(scored),
        "accuracy": sum(1 for e, p in pairs if e == p) / len(pairs) if pairs else 0.0,
        "kappa": cohen_kappa(pairs),
        "per_action": per_action,
        "confusion": Counter(pairs),
        "rote_recall": (sum(1 for r in rote_cases if r.rote_signal == "high") / len(rote_cases)) if rote_cases else None,
        "rote_cases": len(rote_cases),
    }


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def render_report(summary: dict, results: list[CaseResult], model: str, when: datetime) -> str:
    lines = [
        f"# Đo evaluator · {when:%Y-%m-%d %H:%M}",
        "",
        f"Model chấm: `{model}`. Bộ dữ liệu: `evals/evaluator_cases.json` ({summary['total']} câu trả lời gắn nhãn tay"
        f"{f', {summary['errors']} ca lỗi không tính' if summary['errors'] else ''}).",
        "",
        "| Chỉ số | Giá trị |",
        "| --- | --- |",
        f"| Khớp hành động với giáo viên | **{_pct(summary['accuracy'])}** |",
        f"| Cohen's kappa | {summary['kappa']:.2f} |",
        f"| Bắt được dấu hiệu học thuộc (rote_signal = high) | {_pct(summary['rote_recall'])} trên {summary['rote_cases']} ca |",
        "",
        "## Theo từng hành động",
        "",
        "| Hành động | Số ca | Precision | Recall |",
        "| --- | --- | --- | --- |",
    ]
    for action in ACTIONS:
        stats = summary["per_action"][action]
        lines.append(f"| {ACTION_LABELS[action]} | {stats['support']} | {_pct(stats['precision'])} | {_pct(stats['recall'])} |")

    present = [a for a in ACTIONS if any(a in pair for pair in summary["confusion"])]
    lines += [
        "",
        "## Ma trận nhầm lẫn",
        "",
        "Hàng là nhãn tay, cột là quyết định của hệ thống.",
        "",
        "| Nhãn tay \\ Hệ thống | " + " | ".join(ACTION_LABELS[a] for a in present) + " |",
        "| --- |" + " --- |" * len(present),
    ]
    for expected in present:
        cells = [str(summary["confusion"].get((expected, predicted), 0) or "·") for predicted in present]
        lines.append(f"| {ACTION_LABELS[expected]} | " + " | ".join(cells) + " |")

    misses = [r for r in results if not r.correct]
    if misses:
        lines += ["", "## Các ca lệch", "", "| Ca | Nhãn tay | Hệ thống | Nhận xét của evaluator |", "| --- | --- | --- | --- |"]
        for r in misses:
            note = r.error or r.summary
            predicted = ACTION_LABELS.get(r.predicted, r.predicted)
            lines.append(f"| `{r.id}` | {ACTION_LABELS[r.expected]} | {predicted} | {note.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--concurrency", type=int, default=2, help="Số lần gọi Gemini cùng lúc (free tier nên để 1-2)")
    parser.add_argument("--limit", type=int, default=None, help="Chỉ chạy N ca đầu, để thử nhanh")
    args = parser.parse_args()

    if not settings.gemini_api_key:
        raise SystemExit("Chưa có GEMINI_API_KEY trong .env")
    doc, cases = load_cases()
    cases = cases[: args.limit] if args.limit else cases
    results = await run_all(doc, cases, args.concurrency)
    summary = summarize(results)

    when = datetime.now()
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"{when:%Y-%m-%d-%H%M}-{settings.brain_model}"
    report = render_report(summary, results, settings.brain_model, when)
    (RESULTS_DIR / f"{stem}.md").write_text(report, encoding="utf-8")
    (RESULTS_DIR / f"{stem}.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(report)
    print(f"Đã ghi evals/results/{stem}.md")


if __name__ == "__main__":
    asyncio.run(main())

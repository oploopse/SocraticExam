from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    gemini_api_key: str = ""
    app_access_token: str = ""
    brain_model: str = "gemini-3.8-flash"
    fast_model: str = "gemini-3.5-flash-lite"
    live_model: str = "gemini-3.8-live"
    live_voice: str = "Kore"
    vad_silence_ms: int = 1000
    vad_prefix_padding_ms: int = 100
    # "high" bắt câu ngắn nhạy hơn; "low" bớt bị tạp âm làm AI dừng nói (nên dùng khi phòng ồn).
    vad_start_sensitivity: str = "high"
    # Số token suy nghĩ của Gemini Live trước mỗi lượt nói: 0 để AI đáp nhanh nhất, -1 để model tự quyết.
    live_thinking_budget: int = 0
    # Ngôn ngữ chính khi nhận dạng giọng nói, kèm ngôn ngữ phụ của thuật ngữ chuyên ngành.
    speech_language: str = "vi-VN"
    term_languages: str = "en-US"

    # Giới hạn thời gian mỗi lần gọi Gemini (giây); quá giờ thì chuyển sang model dự phòng.
    llm_timeout_s: float = 30
    knowledge_timeout_s: float = 240

    interview_minutes: int = 15
    max_concepts_per_session: int = 6
    max_answers_per_concept: int = 4
    max_hints_per_concept: int = 1

    # Số lượt tối đa mỗi giờ cho một IP, để link demo công khai không làm cạn quota Gemini. 0 = không giới hạn.
    rate_limit_uploads_per_hour: int = 10
    rate_limit_sessions_per_hour: int = 20
    rate_limit_messages_per_hour: int = 300

    max_upload_mb: int = 20
    max_document_chars: int = 200_000
    data_dir: Path = ROOT_DIR / "data"
    # Chuỗi kết nối PostgreSQL (ví dụ Neon). Để trống thì lưu file JSON trong data_dir.
    database_url: str = ""
    web_dir: Path = ROOT_DIR / "web"


settings = Settings()

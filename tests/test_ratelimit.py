from app.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_blocks_after_limit_and_frees_up_after_window():
    clock = FakeClock()
    limiter = RateLimiter(window_s=3600, clock=clock)

    assert limiter.hit("uploads", "1.2.3.4", 2) is None
    clock.now += 600
    assert limiter.hit("uploads", "1.2.3.4", 2) is None
    assert limiter.hit("uploads", "1.2.3.4", 2) == 3000

    clock.now += 3000
    assert limiter.hit("uploads", "1.2.3.4", 2) is None


def test_limiter_counts_each_ip_and_bucket_separately():
    limiter = RateLimiter(clock=FakeClock())

    assert limiter.hit("uploads", "1.1.1.1", 1) is None
    assert limiter.hit("uploads", "1.1.1.1", 1) is not None
    assert limiter.hit("uploads", "2.2.2.2", 1) is None
    assert limiter.hit("sessions", "1.1.1.1", 1) is None


def test_upload_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr("app.main.settings.rate_limit_uploads_per_hour", 2)
    upload = {"file": ("bai.txt", "Nội dung bài học.".encode())}

    assert client.post("/api/documents", files=upload).status_code == 200
    assert client.post("/api/documents", files=upload).status_code == 200
    blocked = client.post("/api/documents", files=upload)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert "2 lượt tải tài liệu" in blocked.json()["detail"]


def test_requests_without_password_do_not_use_up_the_limit(client, monkeypatch):
    monkeypatch.setattr("app.main.settings.app_access_token", "secret")
    monkeypatch.setattr("app.main.settings.rate_limit_uploads_per_hour", 1)
    upload = {"file": ("bai.txt", "Nội dung bài học.".encode())}

    assert client.post("/api/documents", files=upload).status_code == 401
    assert client.post("/api/documents", files=upload).status_code == 401
    response = client.post("/api/documents", files=upload, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200


def test_zero_disables_the_limit(client, monkeypatch):
    monkeypatch.setattr("app.main.settings.rate_limit_uploads_per_hour", 0)
    upload = {"file": ("bai.txt", "Nội dung bài học.".encode())}

    for _ in range(3):
        assert client.post("/api/documents", files=upload).status_code == 200

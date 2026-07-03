from app.core.config import Settings


def test_backend_cors_origins_accepts_comma_separated_env(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")

    settings = Settings()

    assert settings.backend_cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_backend_cors_origins_accepts_json_env(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", '["http://localhost:5173","http://127.0.0.1:5173"]')

    settings = Settings()

    assert settings.backend_cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

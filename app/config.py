from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Neon Postgres
    DATABASE_URL: str

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # SellerCloud
    SELLERCLOUD_BASE_URL: str
    SELLERCLOUD_USERNAME: str
    SELLERCLOUD_PASSWORD: str
    SELLERCLOUD_PO_VIEW_ID: int = 25  # the saved SellerCloud PO view/filter your Apps Script uses

    # CORS
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    class Config:
        env_file = ".env"


settings = Settings()

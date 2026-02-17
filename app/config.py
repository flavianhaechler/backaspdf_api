"""
Configuration - all settings from environment variables.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://localhost/backaspdf"
    
    # Resend
    resend_api_key: str = ""
    resend_webhook_secret: str = ""
    inbound_email: str = "do@backaspdf.com"
    from_email: str = "done@backaspdf.com"
    from_name: str = "Back as PDF"
    notify_email: str = "hey@backaspdf.com"
    contact_email: str = ""
    
    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_pro: str = ""
    
    # URLs
    public_app_url: str = "https://backaspdf.com"
    api_base_url: str = "https://api.backaspdf.com"
    
    # Tokens
    token_signing_secret: str = ""
    portal_token_ttl_minutes: int = 15
    
    # Quotas
    free_monthly_limit: int = 10
    pro_soft_limit: int = 500
    pro_hard_limit: int = 2000
    
    # Processing
    max_inbound_size_mb: int = 10
    max_render_time_seconds: int = 30
    
    # Worker
    worker_poll_interval: float = 1.0
    worker_batch_size: int = 5
    
    # Storage (Future: S3/R2)
    storage_enabled: bool = False
    storage_bucket: str = ""
    storage_endpoint: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""
    
    # App
    environment: str = "production"
    debug: bool = False
    
    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()

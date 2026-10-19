from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Secrets come from the environment, never from source."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "memory"
    auth_mode: str = Field(default="demo", validation_alias=AliasChoices("AUTH_MODE", "ACMG_AUTH_MODE"))
    demo_token: str = Field(
        default="demo-shopper-token",
        validation_alias=AliasChoices("DEMO_TOKEN", "ACMG_DEMO_TOKEN"),
    )
    demo_client_id: str = Field(
        default="demo-client",
        validation_alias=AliasChoices("DEMO_CLIENT_ID", "ACMG_DEMO_CLIENT_ID"),
    )
    demo_client_secret: str = Field(
        default="demo-secret",
        validation_alias=AliasChoices("DEMO_CLIENT_SECRET", "ACMG_DEMO_CLIENT_SECRET"),
    )
    llm_provider: str = "mock"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = "gemini-2.5-flash"
    payment_provider: str = "mock"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_api_base: str = "https://api-m.sandbox.paypal.com"
    public_base_url: str = "http://localhost:4317"
    mcp_internal_url: str = "http://127.0.0.1:4317/mcp"
    allowed_hosts: str = Field(
        default="localhost,localhost:*,127.0.0.1,127.0.0.1:*,[::1],[::1]:*",
        validation_alias=AliasChoices("ALLOWED_HOSTS", "ACMG_ALLOWED_HOSTS"),
    )
    allowed_origins: str = Field(
        default="http://localhost:*,http://127.0.0.1:*,http://[::1]:*",
        validation_alias=AliasChoices("ALLOWED_ORIGINS", "ACMG_ALLOWED_ORIGINS"),
    )
    host: str = "0.0.0.0"
    port: int = 4317
    log_level: str = "INFO"

    @property
    def store_kind(self) -> str:
        return "memory" if self.database_url.strip().lower() == "memory" else "postgres"

    @property
    def mcp_resource(self) -> str:
        return self.public_base_url.rstrip("/") + "/mcp"

    def host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    def origin_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    anthropic_api_key: str
    openai_api_key: str
    langchain_api_key: str = ""
    langchain_tracing_v2: bool = True
    langchain_project: str = "hirelord"
    proxycurl_api_key: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
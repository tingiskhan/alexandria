"""Settings for the S3 source."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class S3Settings(BaseSettings):
    """Bucket, prefix and credentials (ALEXANDRIA_S3_*)."""

    model_config = settings_config("ALEXANDRIA_S3_")

    endpoint_url: str | None = None  # e.g. http://rustfs:9000; None means AWS
    bucket: str
    prefix: str = ""  # sync only keys under this prefix, e.g. "papers/"
    access_key_id: str
    secret_access_key: SecretStr
    region: str = "us-east-1"

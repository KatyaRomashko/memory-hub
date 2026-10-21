"""Settings for the MemoryHub UI backend.

Uses env_prefix="MEMORYHUB_" which matches the memoryhub-db-credentials Secret
naming: MEMORYHUB_DB_HOST, MEMORYHUB_DB_PORT, etc.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORYHUB_",
        case_sensitive=False,
        extra="ignore",
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "memoryhub"
    db_user: str = "memoryhub"
    db_password: str = ""
    embedding_url: str = ""
    mcp_server_url: str = "http://memory-hub-mcp:8080/mcp/"
    auth_service_url: str = "http://auth-server.memoryhub-auth.svc:8081"
    admin_key: str = ""
    cors_origins: str = ""
    # Public-facing route URLs used in the contributor welcome email.
    # These are the URLs an external agent or SDK would connect to from
    # outside the cluster. They differ from mcp_server_url and
    # auth_service_url, which are the internal SVC addresses the BFF
    # itself uses. Populated at deploy time from `oc get route` by
    # memoryhub-ui/deploy/deploy.sh (via `oc set env`). Defaults below
    # are placeholders that surface an obviously wrong URL if the env
    # vars are missing, rather than silently pointing at localhost.
    public_mcp_url: str = "https://mcp-server.example.com/mcp/"
    public_auth_url: str = "https://auth-server.example.com"
    # Tenant this UI deployment serves. All BFF queries filter by this tenant.
    # Set via MEMORYHUB_UI_TENANT_ID env var (one UI instance per tenant --
    # multi-tenant customers run one deployment per tenant). This is the
    # minimal Option A implementation of issue #46 Phase 6 BFF scoping; a
    # future enhancement could replace this with a per-request lookup from
    # the authenticated user. Default is "default" to match the Phase 1
    # server_default on memory_nodes.tenant_id.
    ui_tenant_id: str = "default"

    # --- Dashboard redesign (component health, search playground, login) ---
    # Optional cross-encoder reranker (TEI-compatible POST {"query","texts"}).
    # Empty disables the rerank stage in the search playground and marks the
    # component as "Not configured" on the overview.
    reranker_url: str = ""
    # Same variables the MCP server reads; used only for health probes and the
    # session-focus histogram. Empty = component not deployed.
    valkey_url: str = ""
    s3_endpoint: str = ""
    # Extra trusted token issuers shown on the Clients screen, as a JSON list
    # of {"name", "kind", "description", "status"} objects. The local cluster
    # issuer is derived from the auth service metadata automatically.
    trusted_issuers: str = ""
    # Development-only API-key login (screen 13). When set, every /api route
    # except auth, health and public config requires a session cookie created
    # by POST /api/auth/login. Leave empty behind the OpenShift OAuth proxy.
    ui_dev_api_key: str = ""
    # Label shown in the masthead, e.g. "for OpenShift AI" cluster suffix.
    cluster_domain: str = ""

    # --- Accounts, roles and confidentiality ---
    # Local dashboard accounts (email + password) with self-service sign-up and
    # a first-run admin setup. When true, every /api route except auth, health
    # and public config needs a session, exactly like the dev API-key login.
    # Behind the OpenShift OAuth proxy leave it false: identities come from
    # X-Forwarded-* headers and the admin role from ui_admin_group.
    ui_local_accounts: bool = False
    # HMAC key for session cookies. Set it in production (a Secret). When empty
    # the BFF falls back to the dev API key, then to a key derived from the DB
    # credentials, so sessions survive restarts but rotate with the password.
    ui_session_secret: str = ""
    # Optional one-time token required to create the first platform admin
    # (POST /api/auth/setup). Strongly recommended on shared clusters.
    ui_setup_token: str = ""
    # OpenShift group whose members get the platform-admin role.
    ui_admin_group: str = "memoryhub-admins"
    # Comma-separated OpenShift usernames that are platform admins. Use this
    # when the OAuth proxy does not forward X-Forwarded-Groups.
    ui_admin_users: str = ""

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Runtime settings, read from PORTAL_* environment variables.

On the servers these come from /etc/provisioning-portal/portal.env, which the
Ansible `portal` role writes (secrets from Ansible Vault).
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PORTAL_")

    app_title: str = "Server Provisioning Portal"
    catalog_path: str = "catalog.yaml"

    # Signs the session cookie. Must be identical on both nodes so a VIP
    # failover does not log users out.
    secret_key: str = "change-me"
    session_cookie_secure: bool = False  # set true once nginx serves HTTPS

    # --- Azure DevOps -------------------------------------------------------
    # Submissions are only logged and simulated when dry_run is true.
    dry_run: bool = True
    ado_org_url: str = ""  # https://dev.azure.com/<org> or https://<server>/<collection>
    ado_project: str = ""
    ado_pipeline_id: int = 0
    ado_branch: str = "refs/heads/main"
    ado_api_version: str = "7.1"
    ado_auth: Literal["pat", "service_principal"] = "pat"
    ado_pat: str = ""
    ado_tenant_id: str = ""
    ado_client_id: str = ""
    ado_client_secret: str = ""
    # Path to a CA bundle for an on-prem Azure DevOps Server with an internal CA.
    ado_ca_bundle: str = ""

    # --- User sign-in (Microsoft Entra ID) ----------------------------------
    auth_mode: Literal["entra", "none"] = "entra"
    # "none" is refused outside dry-run unless this is explicitly set.
    allow_anonymous: bool = False
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_redirect_uri: str = ""  # e.g. https://portal.example.com/auth/callback
    # Comma-separated Entra group object IDs or app role names allowed to submit.
    # Empty means any signed-in user of the tenant.
    allowed_groups: str = ""

    @property
    def allowed_group_set(self) -> set[str]:
        return {g.strip() for g in self.allowed_groups.split(",") if g.strip()}

    def check(self) -> None:
        """Refuse to start with a configuration that would be unsafe or useless."""
        if self.auth_mode == "none" and not self.dry_run and not self.allow_anonymous:
            raise RuntimeError(
                "PORTAL_AUTH_MODE=none with PORTAL_DRY_RUN=false lets anyone provision VMs. "
                "Configure Entra ID sign-in, or set PORTAL_ALLOW_ANONYMOUS=true deliberately."
            )
        if self.auth_mode == "entra":
            missing = [
                name
                for name in ("entra_tenant_id", "entra_client_id", "entra_client_secret", "entra_redirect_uri")
                if not getattr(self, name)
            ]
            if missing:
                raise RuntimeError(f"Entra ID sign-in needs: {', '.join('PORTAL_' + m.upper() for m in missing)}")
        if not self.dry_run:
            missing = [n for n in ("ado_org_url", "ado_project") if not getattr(self, n)]
            if not self.ado_pipeline_id:
                missing.append("ado_pipeline_id")
            if self.ado_auth == "pat" and not self.ado_pat:
                missing.append("ado_pat")
            if self.ado_auth == "service_principal":
                missing += [
                    n for n in ("ado_tenant_id", "ado_client_id", "ado_client_secret") if not getattr(self, n)
                ]
            if missing:
                raise RuntimeError(f"Azure DevOps needs: {', '.join('PORTAL_' + m.upper() for m in missing)}")
        if self.secret_key == "change-me" and not self.dry_run:
            raise RuntimeError("Set PORTAL_SECRET_KEY to a long random value (same on both nodes).")


@lru_cache
def get_settings() -> Settings:
    return Settings()

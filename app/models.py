from typing import Optional, List
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.database import Base

# NOTA: La variable 'Base' se importa directamente desde app.database 
# para mantener la compatibilidad con modelos SQLAlchemy si los requieres aquí,
# evitando sobrescribirla o duplicarla.

class LoginRequest(BaseModel):
    username: str
    password: str = Field(..., repr=False)

class UserCreateRequest(BaseModel):
    username: str
    password: str = Field(..., repr=False)
    password_confirm: Optional[str] = Field(None, repr=False)
    is_active: Optional[bool] = True
    roles: Optional[List[str]] = ["viewer"]
    tenants: Optional[List[str]] = []

class UserUpdateRequest(BaseModel):
    username: Optional[str] = None
    is_active: Optional[bool] = None
    roles: Optional[List[str]] = None
    tenants: Optional[List[str]] = None
    password: Optional[str] = Field(None, repr=False)
    password_confirm: Optional[str] = Field(None, repr=False)

class ThemeUpdateRequest(BaseModel):
    theme: str
    avatar_url: Optional[str] = None
    password: Optional[str] = None

class RoleCreateUpdatePayload(BaseModel):
    name: str
    description: Optional[str] = None
    profiles: Optional[List[str]] = []
    tenants: Optional[List[str]] = []

class ProfileCreateUpdatePayload(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None

class TenantPayload(BaseModel):
    name: str
    type: Optional[str] = "metrics"
    account_id: Optional[int] = None
    project_id: Optional[int] = None
    environment: Optional[str] = "default"
    port: Optional[int] = None
    description: Optional[str] = ""
    has_alerts: Optional[bool] = None
    placement_mode: Optional[str] = "manual"  # manual | auto
    vmalert_node_id: Optional[int] = None
    vmalert_port: Optional[int] = None
    display_name: Optional[str] = None
    vmauth_node_id: Optional[int] = None
    vmauth_placement_mode: Optional[str] = "manual"  # manual | auto

class AuthNodePayload(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    host: Optional[str] = None
    ip_address: Optional[str] = None
    service_ip: Optional[str] = None
    type: Optional[str] = None
    component_type: Optional[str] = "vmagent"
    port: Optional[int] = 80
    environment: Optional[str] = "production"
    status: Optional[str] = "operational"
    is_active: Optional[bool] = True
    description: Optional[str] = ""

class DeleteTenantConfirm1(BaseModel):
    reason: Optional[str] = None

class DeleteTenantConfirm2(BaseModel):
    challenge_id: str
    confirm_text: str  # debe coincidir con tenant.slug

class VMAlerterRulesPut(BaseModel):
    yaml: str


class AlertRecipientCreate(BaseModel):
    email: str
    label: Optional[str] = ""
    enabled: Optional[bool] = True


class AlertRecipientUpdate(BaseModel):
    email: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None

class JobStateOut(BaseModel):
    id: str
    type: Optional[str] = None
    ref_id: Optional[int] = None
    status: Optional[str] = None
    phase: Optional[str] = None
    progress_pct: Optional[int] = 0
    logs_ref: Optional[str] = None
    error_code: Optional[str] = None
    result: Optional[dict] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

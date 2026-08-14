"""Provider connection endpoints for sharing LLM credentials across profiles."""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from openhands.agent_server._secrets_exposure import get_config
from openhands.agent_server.persistence import (
    PersistedProviderConnections,
    ProviderConnection,
    get_llm_profile_store,
    get_provider_connections_store,
    get_secrets_store,
    get_settings_store,
)
from openhands.sdk.llm import LLM
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

provider_connections_router = APIRouter(
    prefix="/llm/provider-connections", tags=["LLM Provider Connections"]
)

MAX_PROVIDER_CONNECTIONS = 64
_SECRET_NAME_PREFIX = "llm_provider_connection_"


def _now() -> int:
    return int(time.time())


def _secret_name(connection_id: str) -> str:
    return f"{_SECRET_NAME_PREFIX}{connection_id}"


class ProviderConnectionCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=128)
    provider: str = Field(default="custom", min_length=1, max_length=128)
    api_key: SecretStr = Field(..., min_length=1)
    base_url: str | None = Field(default=None, max_length=2048)

    model_config = ConfigDict(extra="forbid")


class ProviderConnectionUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=2048)

    model_config = ConfigDict(extra="forbid")


class ProviderConnectionResponse(BaseModel):
    id: str
    display_name: str
    provider: str
    base_url: str | None = None
    created_at: int
    updated_at: int
    api_key_set: bool = False


def _to_response(
    connection: ProviderConnection, config=None
) -> ProviderConnectionResponse:
    return ProviderConnectionResponse(
        id=connection.id,
        display_name=connection.display_name,
        provider=connection.provider,
        base_url=connection.base_url,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        api_key_set=_connection_has_api_key(connection, config),
    )


def _find_connection(
    persisted: PersistedProviderConnections | None, connection_id: str
) -> ProviderConnection | None:
    for connection in persisted.connections if persisted else []:
        if connection.id == connection_id:
            return connection
    return None


def _connection_or_404(
    persisted: PersistedProviderConnections | None, connection_id: str
) -> ProviderConnection:
    connection = _find_connection(persisted, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider connection '{connection_id}' not found",
        )
    return connection


def _connection_has_api_key(connection: ProviderConnection, config=None) -> bool:
    value = get_secrets_store(config).get_secret(connection.secret_name)
    return bool(value and value.strip())


def provider_connection_api_key_set(connection_id: str, request: Request) -> bool:
    config = get_config(request)
    connection = _find_connection(
        get_provider_connections_store(config).load(), connection_id
    )
    if connection is None:
        return False
    value = get_secrets_store(config).get_secret(connection.secret_name)
    return bool(value and value.strip())


def _resolve_provider_connection_with_config(llm: LLM, config) -> LLM:
    connection_id = llm.provider_connection_id
    if not connection_id:
        return llm

    connection = _find_connection(
        get_provider_connections_store(config).load(), connection_id
    )
    if connection is None:
        return llm

    updates = {}
    api_key = get_secrets_store(config).get_secret(connection.secret_name)
    if api_key and api_key.strip():
        updates["api_key"] = SecretStr(api_key)
    if connection.base_url is not None:
        updates["base_url"] = connection.base_url
    return llm.model_copy(update=updates) if updates else llm


def resolve_provider_connection(llm: LLM, request: Request) -> LLM:
    return _resolve_provider_connection_with_config(llm, get_config(request))


def _refresh_active_profile_if_linked(config, connection_id: str) -> None:
    settings_store = get_settings_store(config)
    settings = settings_store.load()
    if settings is None or not settings.active_profile:
        return

    try:
        llm = get_llm_profile_store().load(
            settings.active_profile, cipher=config.cipher
        )
    except (FileNotFoundError, TimeoutError, ValueError):
        return
    if llm.provider_connection_id != connection_id:
        return

    resolved = _resolve_provider_connection_with_config(llm, config)

    def refresh(settings):
        settings.agent_settings = settings.agent_settings.model_copy(
            update={"llm": resolved}
        )
        return settings

    settings_store.update(refresh)


def _linked_profile_names(connection_id: str) -> list[str]:
    return sorted(
        str(summary["name"])
        for summary in get_llm_profile_store().list_summaries()
        if summary.get("provider_connection_id") == connection_id
    )


def _active_settings_references_connection(config, connection_id: str) -> bool:
    settings = get_settings_store(config).load()
    if settings is None:
        return False
    return settings.agent_settings.llm.provider_connection_id == connection_id


def _raise_if_connection_is_referenced(config, connection_id: str) -> None:
    profile_names = _linked_profile_names(connection_id)
    active_settings_references = _active_settings_references_connection(
        config, connection_id
    )
    if not profile_names and not active_settings_references:
        return

    reasons = []
    if profile_names:
        reasons.append(f"referenced by LLM profile(s): {', '.join(profile_names)}")
    if active_settings_references:
        reasons.append("copied into active settings")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Provider connection cannot be deleted while it is "
            + " and ".join(reasons)
            + ". Update those references before deleting it."
        ),
    )


@provider_connections_router.get("", response_model=list[ProviderConnectionResponse])
async def list_provider_connections(
    request: Request,
) -> list[ProviderConnectionResponse]:
    config = get_config(request)
    store = get_provider_connections_store(config)
    persisted = store.load()
    return [
        _to_response(c, config) for c in (persisted.connections if persisted else [])
    ]


@provider_connections_router.post(
    "", response_model=ProviderConnectionResponse, status_code=status.HTTP_201_CREATED
)
async def create_provider_connection(
    request: Request, body: ProviderConnectionCreateRequest
) -> ProviderConnectionResponse:
    config = get_config(request)
    store = get_provider_connections_store(config)
    secrets_store = get_secrets_store(config)
    connection_id = uuid.uuid4().hex
    secret_name = _secret_name(connection_id)
    now = _now()

    def add(
        persisted: PersistedProviderConnections,
    ) -> PersistedProviderConnections:
        if len(persisted.connections) >= MAX_PROVIDER_CONNECTIONS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Provider connection limit reached ({MAX_PROVIDER_CONNECTIONS}). "
                    "Delete one before adding another."
                ),
            )
        persisted.connections.append(
            ProviderConnection(
                id=connection_id,
                display_name=body.display_name,
                provider=body.provider,
                secret_name=secret_name,
                base_url=body.base_url,
                created_at=now,
                updated_at=now,
            )
        )
        return persisted

    secrets_store.set_secret(
        name=secret_name,
        value=body.api_key.get_secret_value(),
        description=f"LLM provider connection key for {body.display_name}",
    )
    try:
        persisted = store.update(add)
    except Exception:
        secrets_store.delete_secret(secret_name)
        raise

    connection = _connection_or_404(persisted, connection_id)
    logger.info(
        "Created LLM provider connection", extra={"connection_id": connection_id}
    )
    return _to_response(connection, config)


@provider_connections_router.patch(
    "/{connection_id}", response_model=ProviderConnectionResponse
)
async def update_provider_connection(
    request: Request, connection_id: str, body: ProviderConnectionUpdateRequest
) -> ProviderConnectionResponse:
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one provider connection field to update",
        )

    config = get_config(request)
    store = get_provider_connections_store(config)
    secrets_store = get_secrets_store(config)

    def patch(
        persisted: PersistedProviderConnections,
    ) -> PersistedProviderConnections:
        connection = _connection_or_404(persisted, connection_id)
        if "api_key" in fields and body.api_key is not None:
            secrets_store.set_secret(
                name=connection.secret_name,
                value=body.api_key.get_secret_value(),
                description=f"LLM provider key for {connection.display_name}",
            )

        updates = {"updated_at": _now()}
        for field in ("display_name", "provider", "base_url"):
            if field in fields:
                updates[field] = getattr(body, field)
        updated = connection.model_copy(update=updates)
        persisted.connections = [
            updated if c.id == connection_id else c for c in persisted.connections
        ]
        return persisted

    persisted = store.update(patch)
    _refresh_active_profile_if_linked(config, connection_id)
    return _to_response(_connection_or_404(persisted, connection_id), config)


@provider_connections_router.delete(
    "/{connection_id}", response_model=ProviderConnectionResponse
)
async def delete_provider_connection(
    request: Request, connection_id: str
) -> ProviderConnectionResponse:
    config = get_config(request)
    store = get_provider_connections_store(config)
    secrets_store = get_secrets_store(config)
    _connection_or_404(store.load(), connection_id)
    _raise_if_connection_is_referenced(config, connection_id)
    removed: dict[str, ProviderConnection] = {}

    def remove(
        persisted: PersistedProviderConnections,
    ) -> PersistedProviderConnections:
        connection = _connection_or_404(persisted, connection_id)
        removed["connection"] = connection
        persisted.connections = [
            c for c in persisted.connections if c.id != connection_id
        ]
        return persisted

    store.update(remove)
    connection = removed["connection"]
    secrets_store.delete_secret(connection.secret_name)
    logger.info(
        "Deleted LLM provider connection", extra={"connection_id": connection_id}
    )
    return ProviderConnectionResponse(
        id=connection.id,
        display_name=connection.display_name,
        provider=connection.provider,
        base_url=connection.base_url,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        api_key_set=False,
    )

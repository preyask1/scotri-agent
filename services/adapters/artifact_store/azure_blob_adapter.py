# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""ArtifactStore on Azure Blob Storage — the Azure/Foundry deployment (ARTIFACT_STORE=azure_blob).

Shared by every API/MCP replica, unlike the local adapter, so a file one pod published is readable
from any other. Needs `azure-storage-blob` and `azure-identity` (services/requirements-azure.txt);
imported lazily so a Mac/Windows install that never selects this adapter doesn't need them.

Auth, in order of precedence. Every option is non-interactive, per the hard rule against browser or
per-user-cache auth. DefaultAzureCredential is deliberately NOT used: its chain falls through to the
Azure CLI's cached `az login` token, which is exactly the per-user credential this workspace rules out.
  1. ARTIFACT_STORE_AZURE_CONNECTION_STRING — simplest; also how a local Azurite emulator is reached.
  2. ARTIFACT_STORE_AZURE_ACCOUNT_URL + ARTIFACT_STORE_AZURE_TENANT_ID/_CLIENT_ID/_CLIENT_SECRET —
     a service principal with "Storage Blob Data Contributor" on the container.
  3. ARTIFACT_STORE_AZURE_ACCOUNT_URL alone — managed identity (AKS workload identity, App Service,
     Foundry compute); ARTIFACT_STORE_AZURE_MANAGED_IDENTITY_CLIENT_ID picks a user-assigned one.

Blob metadata only allows ASCII, so values are percent-encoded on write and decoded on read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, unquote

from domain.errors import ConfigurationError
from ports.artifact_store import StoredFile
from ports.secret_provider import SecretProvider

DEFAULT_CONTAINER = "scotri-artifacts"


def _encode(metadata: dict[str, str]) -> dict[str, str]:
    return {k: quote(v, safe="") for k, v in metadata.items()}


def _decode(metadata: dict[str, str] | None) -> dict[str, str]:
    return {k: unquote(v) for k, v in (metadata or {}).items()}


class AzureBlobArtifactStore:
    """Implements ports.artifact_store.ArtifactStore."""

    def __init__(self, secrets: SecretProvider) -> None:
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise ConfigurationError(
                "ARTIFACT_STORE=azure_blob needs azure-storage-blob and azure-identity: "
                "pip install -r requirements-azure.txt"
            ) from exc

        container = secrets.get_optional("ARTIFACT_STORE_AZURE_CONTAINER", DEFAULT_CONTAINER)
        connection_string = secrets.get_optional("ARTIFACT_STORE_AZURE_CONNECTION_STRING")
        if connection_string:
            service = BlobServiceClient.from_connection_string(connection_string)
        else:
            account_url = secrets.get("ARTIFACT_STORE_AZURE_ACCOUNT_URL")
            service = BlobServiceClient(account_url=account_url, credential=self._credential(secrets))
        self._container = service.get_container_client(container)

    @staticmethod
    def _credential(secrets: SecretProvider):
        from azure.identity import ClientSecretCredential, ManagedIdentityCredential

        client_secret = secrets.get_optional("ARTIFACT_STORE_AZURE_CLIENT_SECRET")
        if client_secret:
            return ClientSecretCredential(
                tenant_id=secrets.get("ARTIFACT_STORE_AZURE_TENANT_ID"),
                client_id=secrets.get("ARTIFACT_STORE_AZURE_CLIENT_ID"),
                client_secret=client_secret,
            )
        client_id = secrets.get_optional("ARTIFACT_STORE_AZURE_MANAGED_IDENTITY_CLIENT_ID")
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()

    @staticmethod
    def _record(key: str, size: int, content_type: str | None, created: datetime | None, metadata) -> StoredFile:
        return StoredFile(
            key=key, size=size, content_type=content_type or "application/octet-stream",
            created_at=created or datetime.now(timezone.utc), metadata=_decode(metadata),
        )

    def put(self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> StoredFile:
        from azure.storage.blob import ContentSettings

        self._container.upload_blob(
            name=key, data=data, overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            metadata=_encode(metadata or {}),
        )
        return self._record(key, len(data), content_type, datetime.now(timezone.utc), _encode(metadata or {}))

    def head(self, key: str) -> StoredFile | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            props = self._container.get_blob_client(key).get_blob_properties()
        except ResourceNotFoundError:
            return None
        return self._record(key, props.size, props.content_settings.content_type, props.creation_time, props.metadata)

    def get(self, key: str) -> tuple[bytes, StoredFile] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = self._container.get_blob_client(key).download_blob()
        except ResourceNotFoundError:
            return None
        props = downloader.properties
        record = self._record(key, props.size, props.content_settings.content_type, props.creation_time, props.metadata)
        return downloader.readall(), record

    def delete(self, key: str) -> bool:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._container.delete_blob(key, delete_snapshots="include")
        except ResourceNotFoundError:
            return False
        return True

    def list(self, prefix: str) -> list[StoredFile]:
        return sorted(
            (
                self._record(b.name, b.size, b.content_settings.content_type, b.creation_time, b.metadata)
                for b in self._container.list_blobs(name_starts_with=prefix, include=["metadata"])
            ),
            key=lambda r: r.key,
        )

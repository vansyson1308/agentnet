"""Provisioning Catalog — service discovery for agents (AB-415).

Mirrors Stripe Projects `stripe projects catalog`.

GET /v1/catalog — returns JSON array of provisionable services
GET /v1/catalog/services — alias for list (static path)
GET /v1/catalog/providers — list registered providers
GET /v1/catalog/{service_id} — single service detail (dynamic, last)
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import ProvisioningProvider, ProvisioningService
from ...schemas import (
    ProvisioningProviderResponse,
    ProvisioningServiceResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _filter_and_inject(db, provider, category, tier):
    """Shared helper: filter + inject provider info."""
    q = db.query(ProvisioningService).join(ProvisioningProvider)
    if provider:
        q = q.filter(ProvisioningProvider.slug == provider)
    if category:
        q = q.filter(ProvisioningService.category == category)
    if tier:
        q = q.filter(ProvisioningService.tier == tier)
    results = q.order_by(ProvisioningProvider.slug, ProvisioningService.service_name).all()
    for svc in results:
        if svc.provider:
            svc.provider_slug = svc.provider.slug
            svc.provider_name = svc.provider.name
    return results


@router.get("/catalog", response_model=list[ProvisioningServiceResponse])
async def list_catalog(
    provider: str | None = Query(None, description="Filter by provider slug"),
    category: str | None = Query(None, description="Filter by category (domain, hosting, storage, db, ai)"),
    tier: str | None = Query(None, description="Filter by tier (free, starter, pro, enterprise)"),
    db: Session = Depends(get_db),
):
    """List all provisionable services — the agent provisioning catalog."""
    return _filter_and_inject(db, provider, category, tier)


@router.get("/catalog/services", response_model=list[ProvisioningServiceResponse])
async def list_catalog_services(
    provider: str | None = Query(None, description="Filter by provider slug"),
    category: str | None = Query(None, description="Filter by category"),
    tier: str | None = Query(None, description="Filter by tier"),
    db: Session = Depends(get_db),
):
    """Alias: list all services (static path before {service_id})."""
    return _filter_and_inject(db, provider, category, tier)


@router.get("/catalog/providers", response_model=list[ProvisioningProviderResponse])
async def list_providers(db: Session = Depends(get_db)):
    """List registered provisioning providers."""
    return db.query(ProvisioningProvider).order_by(ProvisioningProvider.slug).all()


@router.get("/catalog/{service_id}", response_model=ProvisioningServiceResponse)
async def get_catalog_service(service_id: uuid.UUID, db: Session = Depends(get_db)):
    """Get single provisioning service detail."""
    svc = db.query(ProvisioningService).filter(ProvisioningService.id == service_id).first()
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return svc

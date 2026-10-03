# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Owner-only admin panel API: installation summary and the published-model catalog.

Account lifecycle stays in ``routes.accounts``; loading, unloading and deleting models stays in
the inference and models routes. This router adds only what had no home: the catalog of models other accounts may use, and their alias names.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import model_policy, policy, storage
from auth.authentication import get_current_subject

router = APIRouter(dependencies = [Depends(get_current_subject), Depends(policy.require_owner)])


class CatalogEntry(BaseModel):
    model_id: str = Field(..., min_length = 1, max_length = 300)
    alias: str = Field("", max_length = 200, description = "Blank gets a generated alias")


class ModelCatalogResponse(BaseModel):
    models: List[CatalogEntry]


class ModelCatalogRequest(BaseModel):
    models: List[CatalogEntry] = Field(default_factory = list, max_length = 500)


class AdminOverviewResponse(BaseModel):
    total_accounts: int
    active_accounts: int
    published_models: int


@router.get("/overview", response_model = AdminOverviewResponse)
def overview():
    accounts = storage.list_accounts()
    return {
        "total_accounts": len(accounts),
        "active_accounts": sum(1 for a in accounts if a.get("is_active")),
        "published_models": len(model_policy.get_catalog()),
    }


@router.get("/model-catalog", response_model = ModelCatalogResponse)
def get_model_catalog():
    return {"models": model_policy.get_catalog()}


@router.put("/model-catalog", response_model = ModelCatalogResponse)
def put_model_catalog(payload: ModelCatalogRequest):
    try:
        return {"models": model_policy.set_catalog([m.model_dump() for m in payload.models])}
    except ValueError as exc:
        raise HTTPException(status_code = 400, detail = str(exc))

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import { authFetch } from "@/features/auth";
import { readFastApiError } from "@/lib/format-fastapi-error";

export interface CatalogEntry {
  model_id: string;
  alias: string;
}

async function adminRequest(path: string, init?: RequestInit): Promise<Response> {
  const response = await authFetch(`/api/admin${path}`, init);
  if (!response.ok) {
    throw new Error(await readFastApiError(response, "Admin request failed"));
  }
  return response;
}

export async function fetchCatalog(): Promise<CatalogEntry[]> {
  const response = await adminRequest("/model-catalog");
  return ((await response.json()) as { models: CatalogEntry[] }).models;
}

/** Replaces the whole catalog; a blank alias is replaced by a generated one. */
export async function saveCatalog(models: CatalogEntry[]): Promise<CatalogEntry[]> {
  const response = await adminRequest("/model-catalog", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models }),
  });
  return ((await response.json()) as { models: CatalogEntry[] }).models;
}

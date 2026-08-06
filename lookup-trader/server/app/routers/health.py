"""Operational data/model health endpoints."""

from fastapi import APIRouter

from app.services.data_health import data_model_health

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/data-model")
def get_data_model_health():
    return data_model_health()

"""Operational data/model health endpoints."""

from fastapi import APIRouter

from app.config import settings
from app.services.data_health import data_model_health
from app.services.market_execution import execution_status
from app.services.meta_shadow_store import MetaShadowStore

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/data-model")
def get_data_model_health():
    return {
        **data_model_health(),
        "execution": execution_status(settings, MetaShadowStore(settings.meta_shadow_db_path)),
    }

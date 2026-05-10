from fastapi import APIRouter

from ..database import get_daily_metrics

router = APIRouter()


@router.get("/performance")
def get_performance():
    metrics = get_daily_metrics()
    return {"metrics": metrics}

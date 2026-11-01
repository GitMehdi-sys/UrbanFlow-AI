"""Water balance forecasts from explicitly supplied measurements, without seeded data."""
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

router = APIRouter(prefix='/api/water', tags=['water'])
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Measurements(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=200)
    observed_at: AwareDatetime
    production_m3_day: Nonnegative
    demand_m3_day: Nonnegative
    storage_m3: Nonnegative
    storage_capacity_m3: Annotated[float, Field(gt=0, allow_inf_nan=False)]

    @model_validator(mode='after')
    def validate_storage(self):
        if self.storage_m3 > self.storage_capacity_m3:
            raise ValueError('Storage cannot exceed storage capacity')
        return self


class Forecast(BaseModel):
    horizon_hours: int = Field(default=24, ge=1, le=168)
    production_loss_percent: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    demand_reduction_percent: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)


def measurement_path():
    from agentsumo.core import AgentSUMOConfig
    return AgentSUMOConfig.OUTPUT_DIR / 'water' / 'measurements.json'


def load_measurements():
    path = measurement_path()
    if not path.exists():
        raise HTTPException(409, 'Import water measurements before calculating a forecast.')
    try:
        return Measurements.model_validate_json(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, 'Stored water measurements are invalid. Import a corrected file.') from exc


@router.get('/measurements')
def get_measurements():
    return load_measurements()


@router.put('/measurements')
def import_measurements(measurements: Measurements):
    path = measurement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    temp = path.with_name(f'.measurements-{uuid4().hex}.tmp')
    try:
        temp.write_text(measurements.model_dump_json(indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return measurements


@router.post('/forecast')
def forecast(request: Forecast):
    measured = load_measurements()
    supply = measured.production_m3_day * (1 - request.production_loss_percent / 100)
    demand = measured.demand_m3_day * (1 - request.demand_reduction_percent / 100)
    stored, unmet, spilled = measured.storage_m3, 0.0, 0.0
    timeline = []
    for hour in range(request.horizon_hours + 1):
        timeline.append({'hour': hour, 'storage_m3': round(stored, 3), 'unmet_m3': round(unmet, 3), 'overflow_m3': round(spilled, 3)})
        if hour < request.horizon_hours:
            balance = stored + (supply - demand) / 24
            unmet += max(0, -balance)
            spilled += max(0, balance - measured.storage_capacity_m3)
            stored = min(measured.storage_capacity_m3, max(0, balance))
    return {'measurements': measured.model_dump(mode='json'), 'scenario': request.model_dump(),
            'production_m3_day': supply, 'demand_m3_day': demand, 'timeline': timeline,
            'method': 'Hourly storage mass balance with constant production and demand. Forecast, not a hydraulic model or live telemetry.'}

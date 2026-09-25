from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List

from ..database import get_db
from .. import schemas
from ..enums import ResourceType, ReservationStatus, ProjectStatus
from ..errors import HTTPStatus, ERROR_NOT_FOUND
from ..services import capacity_ledger
from ..services.capacity_ledger import LedgerError, LedgerNotFound

router = APIRouter(prefix="/capacity-ledger", tags=["园区容量预约台账"])


def _run(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except LedgerNotFound as e:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(e)) from e
    except LedgerError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e)) from e


@router.post(
    "/capacities",
    response_model=schemas.ParkResourceCapacityOut,
    summary="登记园区资源容量（追加新版本，历史版本不可改）",
)
def register_capacity(
    cap_in: schemas.ParkResourceCapacityCreate,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.register_capacity, db, cap_in)


@router.get(
    "/capacities",
    response_model=List[schemas.ParkResourceCapacityOut],
    summary="查询园区资源容量版本列表",
)
def list_capacities(
    park_id: Optional[int] = Query(None),
    resource_type: Optional[ResourceType] = Query(None),
    db: Session = Depends(get_db),
):
    return capacity_ledger.list_capacities(
        db, park_id=park_id, resource_type=resource_type
    )


@router.get(
    "/parks/{park_id}/availability",
    response_model=schemas.AvailabilityResponse,
    summary="查询园区资源逐月可用量（总量/已承诺/剩余/来源承诺）",
)
def get_availability(
    park_id: int,
    resource_type: ResourceType = Query(...),
    start_year: int = Query(...),
    start_month: int = Query(...),
    end_year: int = Query(...),
    end_month: int = Query(...),
    db: Session = Depends(get_db),
):
    return _run(
        capacity_ledger.get_availability,
        db,
        park_id,
        resource_type,
        start_year,
        start_month,
        end_year,
        end_month,
    )


@router.post(
    "/reservations",
    response_model=schemas.CapacityReservationDetail,
    summary="登记容量预约（暂存）",
)
def create_reservation(
    reservation_in: schemas.CapacityReservationCreate,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.create_draft, db, reservation_in)


@router.get(
    "/reservations",
    response_model=List[schemas.CapacityReservationOut],
    summary="查询容量预约列表",
)
def list_reservations(
    park_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    resource_type: Optional[ResourceType] = Query(None),
    status: Optional[ReservationStatus] = Query(None),
    stage: Optional[ProjectStatus] = Query(None),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return capacity_ledger.list_reservations(
        db,
        park_id=park_id,
        project_id=project_id,
        resource_type=resource_type,
        status=status,
        stage=stage,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/reservations/confirm",
    response_model=schemas.ConfirmResponse,
    summary="批量确认预约（一笔事务内校验总量与既有承诺，拒绝项含来源承诺与剩余量）",
)
def confirm_reservations(
    confirm_in: schemas.ConfirmRequest,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.confirm_reservations, db, confirm_in)


@router.get(
    "/reservations/{reservation_id}",
    response_model=schemas.CapacityReservationDetail,
    summary="查询预约详情（含月度占用与台账事件）",
)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    reservation = capacity_ledger.get_reservation(db, reservation_id)
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation


@router.put(
    "/reservations/{reservation_id}",
    response_model=schemas.CapacityReservationDetail,
    summary="修订暂存预约（已确认的台账不可原地修订）",
)
def update_reservation(
    reservation_id: int,
    reservation_in: schemas.CapacityReservationUpdate,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.update_draft, db, reservation_id, reservation_in)


@router.post(
    "/reservations/{reservation_id}/release",
    response_model=schemas.ReleaseResponse,
    summary="释放预约额度（支持按月部分释放；历史月份封存不可修订）",
)
def release_reservation(
    reservation_id: int,
    release_in: schemas.ReleaseRequest,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.release_reservation, db, reservation_id, release_in)


@router.post(
    "/reservations/{reservation_id}/transfer",
    response_model=schemas.TransferResponse,
    summary="将预约额度转移给同园区其他项目",
)
def transfer_reservation(
    reservation_id: int,
    transfer_in: schemas.TransferRequest,
    db: Session = Depends(get_db),
):
    return _run(capacity_ledger.transfer_reservation, db, reservation_id, transfer_in)


@router.get(
    "/reservations/{reservation_id}/events",
    response_model=List[schemas.CapacityLedgerEventOut],
    summary="查询预约的台账事件（追加式审计日志）",
)
def list_reservation_events(reservation_id: int, db: Session = Depends(get_db)):
    reservation = capacity_ledger.get_reservation(db, reservation_id)
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation.events


@router.post(
    "/projects/{project_id}/withdraw",
    response_model=schemas.WithdrawResponse,
    summary="项目撤回：按规则释放该项目全部已确认预约的未使用额度",
)
def withdraw_project(
    project_id: int,
    withdraw_in: schemas.WithdrawRequest,
    db: Session = Depends(get_db),
):
    return _run(
        capacity_ledger.withdraw_project_reservations, db, project_id, withdraw_in
    )

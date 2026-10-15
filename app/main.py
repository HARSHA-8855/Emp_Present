"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
VALID_PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


def parse_hhmm(value: str) -> tuple[int, int]:
    h_str, m_str = value.split(":")
    return int(h_str), int(m_str)


def to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def dt_from_epoch_ms(value: int | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def get_shift_context(emp: dict[str, Any], punch_dt_utc: datetime) -> tuple[date, datetime, datetime, datetime]:
    punch_ist = punch_dt_utc.astimezone(IST)
    start_h, start_m = parse_hhmm(emp["shift_start"])
    end_h, end_m = parse_hhmm(emp["shift_end"])

    attendance_date = punch_ist.date()
    if emp["shift_end"] <= emp["shift_start"] and punch_ist.time() < time(end_h, end_m):
        attendance_date -= timedelta(days=1)

    shift_start = datetime.combine(attendance_date, time(start_h, start_m), tzinfo=IST)
    end_date = attendance_date + timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else attendance_date
    shift_end = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    return attendance_date, punch_ist, shift_start, shift_end


def compute_late_minutes(punch_ist: datetime, shift_start: datetime) -> int:
    delta_minutes = int((punch_ist - shift_start).total_seconds() // 60)
    if delta_minutes > 10:
        return delta_minutes
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    delta_seconds = Decimal(str((punch_out_utc - punch_in_utc).total_seconds()))
    hours = delta_seconds / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime_minutes(punch_out_ist: datetime, shift_end: datetime) -> int:
    delta_seconds = (punch_out_ist - shift_end).total_seconds()
    if delta_seconds < 0:
        return 0
    minutes = int(delta_seconds // 60)
    if minutes >= 30:
        return minutes
    return 0


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    if "at" in out and isinstance(out["at"], datetime):
        out["at"] = to_epoch_ms(out["at"])
    if "changes" in out and isinstance(out["changes"], dict):
        for field_name, details in list(out["changes"].items()):
            if not isinstance(details, dict):
                continue
            for key in ("from", "to"):
                value = details.get(key)
                if isinstance(value, datetime):
                    details[key] = to_epoch_ms(value)
    return out


def serialize_attendance_record(doc: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in doc.items() if key != "_id"}

    for key in ("punch_in", "punch_out"):
        if key in out and out[key] is not None and isinstance(out[key], datetime):
            out[key] = to_epoch_ms(out[key])

    if "history" in out and isinstance(out["history"], list):
        out["history"] = [serialize_history_entry(item) for item in out["history"]]

    if "work_hours" in out and out["work_hours"] is not None:
        out["work_hours"] = float(out["work_hours"])

    return out


@app.on_event("startup")
def ensure_startup_indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1)], name="idx_employees_department")
    db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_employees_department_emp_code")

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_logs_emp_code_date"
    )
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)], name="idx_attendance_logs_date_emp_code")
    db.attendance_logs.create_index([("status", 1)], name="idx_attendance_logs_status")


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def validate_shift_window(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: int | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("punched_at must be an integer epoch millisecond value")
        if value < 100_000_000_000 or value > 4_102_444_800_000:
            raise ValueError("punched_at must be a valid epoch millisecond timestamp")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: int | None = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("punched_at must be an integer epoch millisecond value")
        if value < 100_000_000_000 or value > 4_102_444_800_000:
            raise ValueError("punched_at must be a valid epoch millisecond timestamp")
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: int | None = None
    punch_out: int | None = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_millis(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("punch_in and punch_out must be epoch millisecond integers")
        if value < 100_000_000_000 or value > 4_102_444_800_000:
            raise ValueError("epoch milliseconds out of range")
        return value


class EmployeePage(BaseModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: int | None
    punch_out: int | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[dict[str, Any]]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}

    if "created_at" in out and isinstance(out["created_at"], datetime):
        out["created_at"] = int(out["created_at"].timestamp() * 1000)

    if "joined_on" in out and isinstance(out["joined_on"], date):
        out["joined_on"] = out["joined_on"].isoformat()

    return out


def month_start_and_end(month: str) -> tuple[date, date]:
    year_str, month_str = month.split("-")
    year = int(year_str)
    month_num = int(month_str)
    start = date(year, month_num, 1)
    if month_num == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_num + 1, 1)
    last_day = (next_month - timedelta(days=1))
    return start, last_day


def working_days_in_month(joined_on: str, month: str) -> int:
    joined_date = date.fromisoformat(joined_on)
    start, end = month_start_and_end(month)
    current = max(start, joined_date)
    count = 0
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def quarter_up(value: Decimal, places: str) -> Decimal:
    q = Decimal(places)
    return value.quantize(q, rounding=ROUND_HALF_UP)


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_start_and_end(month)
    next_month = end + timedelta(days=1)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, [1, 2, 3, 4, 5]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$cond": [{"$gt": ["$overtime_minutes", 0]}, "$overtime_minutes", 0]}},
            }
        },
        {"$project": {"_id": 0}},
    ]


def build_department_summary_pipeline(month: str, department: str | None = None) -> list[dict[str, Any]]:
    start, end = month_start_and_end(month)
    next_month = end + timedelta(days=1)
    stages = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department} if department is not None else {}},
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, [1, 2, 3, 4, 5]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "avg_work_hours_total": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "avg_work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"dept": "$_id"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", end.isoformat()]}]}}},
                    {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
                ],
                "as": "headcount_docs",
            }
        },
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$ifNull": [{"$first": "$headcount_docs.headcount"}, 0]}, "present_days": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1, "avg_work_hours_total": 1, "avg_work_hours_count": 1}},
        {"$match": {"headcount": {"$gt": 0}}},
    ]
    return stages


def build_late_leaderboard_pipeline(month: str, department: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    start, end = month_start_and_end(month)
    next_month = end + timedelta(days=1)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$total_late_minutes",
                "members": {
                    "$push": {
                        "emp_code": "$_id",
                        "name": "$name",
                        "department": "$department",
                        "total_late_minutes": "$total_late_minutes",
                        "late_count": "$late_count",
                    }
                },
                "group_size": {"$sum": 1},
            }
        },
        {"$sort": {"_id": -1}},
        {
            "$setWindowFields": {
                "sortBy": {"_id": -1},
                "output": {
                    "running_total": {"$sum": "$group_size", "window": {"documents": ["unbounded", "current"]}},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": {"$add": [{"$subtract": ["$running_total", "$group_size"]}, 1]},
                "members": 1,
            }
        },
        {"$unwind": "$members"},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$members.emp_code",
                "name": "$members.name",
                "department": "$members.department",
                "total_late_minutes": "$members.total_late_minutes",
                "late_count": "$members.late_count",
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": end.isoformat()},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "joined_on": 1,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {"$replaceRoot": {"newRoot": "$logs"}},
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - depends on Mongo runtime
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=status.HTTP_201_CREATED, response_model=EmployeeResponse)
def create_employee(body: EmployeeCreate) -> EmployeeResponse:
    doc = body.model_dump(exclude_none=True)
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc

    return EmployeeResponse.model_validate(serialize_employee(doc))


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: str | None = Query(default=None, description="Exact, case-sensitive match."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> EmployeePage:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [EmployeeResponse.model_validate(serialize_employee(doc)) for doc in cursor]

    return EmployeePage(items=items, total=total, page=page, page_size=page_size)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> EmployeeMonthly:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    result = next(db.attendance_logs.aggregate(pipeline), None)
    if result is None:
        result = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    working_days = working_days_in_month(employee["joined_on"], month)
    present_days = Decimal(str(result.get("present_days", 0)))
    leave_days = int(result.get("leave_days", 0))
    late_count = int(result.get("late_count", 0))
    total_late_minutes = int(result.get("total_late_minutes", 0))
    total_overtime_minutes = int(result.get("total_overtime_minutes", 0))

    attendance_pct: float | None = None
    if working_days > 0:
        ratio = (present_days / Decimal(working_days)) * Decimal("100")
        attendance_pct = float(quarter_up(ratio, "0.01"))

    payload = {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }
    return EmployeeMonthly.model_validate(payload)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> AttendancePage:
    if date_from is not None and date_to is not None and date.fromisoformat(date_from) > date.fromisoformat(date_to):
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    items = [AttendanceRecord.model_validate(serialize_attendance_record(doc)) for doc in cursor]
    return AttendancePage(items=items, total=total, page=page, page_size=page_size)


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = Query(default=None),
) -> DepartmentSummary:
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.attendance_logs.aggregate(pipeline))
    for row in rows:
        if row.get("headcount", 0) == 0:
            continue
        row["avg_work_hours"] = None if row.get("avg_work_hours_count", 0) == 0 else float(quarter_up(Decimal(str(row["avg_work_hours_total"])) / Decimal(row["avg_work_hours_count"]), "0.01"))
        row["present_days"] = float(row.get("present_days", 0))
    result = {
        "month": month,
        "items": [
            {
                "department": row["department"],
                "headcount": int(row.get("headcount", 0)),
                "present_days": float(row.get("present_days", 0)),
                "avg_work_hours": row.get("avg_work_hours"),
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
            for row in rows
        ],
    }
    return DepartmentSummary.model_validate(result)


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = Query(default=None),
) -> Leaderboard:
    pipeline = build_late_leaderboard_pipeline(month, department, limit)
    rows = list(db.attendance_logs.aggregate(pipeline))
    result = {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in rows
        ],
    }
    return Leaderboard.model_validate(result)


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> Trend:
    start = date.fromisoformat(from_)
    end = date.fromisoformat(to)
    if end < start:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")

    employee_count = db.employees.count_documents({"department": department, "joined_on": {"$lte": end.isoformat()}})
    if employee_count == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    current = list(db.attendance_logs.aggregate(build_department_trend_pipeline(department, from_, to)))
    by_date = {row["date"]: row for row in current}
    items = []
    cursor_date = start
    while cursor_date <= end:
        date_str = cursor_date.isoformat()
        row = by_date.get(date_str, {"present_count": 0, "late_count": 0})
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": date_str}})
        is_working_day = cursor_date.weekday() < 5
        present_count = float(row.get("present_count", 0))
        late_count = int(row.get("late_count", 0))
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = float(quarter_up((Decimal(str(present_count)) / Decimal(headcount)) * Decimal("100"), "0.0001"))
            if attendance_rate == 0.0 and present_count == 0:
                attendance_rate = 0.0
        items.append(
            {
                "date": date_str,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": None,
            }
        )
        cursor_date += timedelta(days=1)

    history = []
    window = []
    for item in items:
        if item["attendance_rate"] is not None:
            window.append(item["attendance_rate"])
        if len(window) > 7:
            window.pop(0)
        avg = None
        if window:
            avg = float(quarter_up(sum(Decimal(str(v)) for v in window) / Decimal(len(window)), "0.0001"))
        item["moving_avg_7d"] = avg
        history.append(item)
    return Trend(department=department, items=history)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = Query(default=None),
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=50),
    date_from: str | None = Query(default=None, alias="date_from"),
    date_to: str | None = Query(default=None, alias="date_to"),
    status: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=404, detail="unknown endpoint")

    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from is not None or date_to is not None:
            q["date"] = {}
            if date_from is not None:
                q["date"]["$gte"] = date_from
            if date_to is not None:
                q["date"]["$lte"] = date_to
        collection = "attendance_logs"
        cmd = {"explain": {"find": collection, "filter": q, "sort": [("date", -1), ("emp_code", 1)], "skip": (page - 1) * page_size, "limit": page_size}, "verbosity": "executionStats"}
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, department, limit or 10)
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    else:
        if from_ is None or to is None:
            raise HTTPException(status_code=422, detail="from and to are required")
        collection = "attendance_logs"
        pipeline = build_department_trend_pipeline(department or "", from_, to)
        cmd = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}

    explain = db.command(cmd)
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = dt_from_epoch_ms(body.punched_at)
    attendance_date, punch_ist, shift_start, shift_end = get_shift_context(employee, punched_at)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_ist, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        inserted = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc

    created = db.attendance_logs.find_one({"_id": inserted.inserted_id})
    if created is None:
        raise HTTPException(status_code=500, detail="failed to load created attendance record")
    return serialize_attendance_record(created)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = dt_from_epoch_ms(body.punched_at)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    punch_in_dt = record["punch_in"]
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

    if (punched_at - punch_in_dt).total_seconds() > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    _, _, _, shift_end = get_shift_context(employee, punch_in_dt)
    work_hours = compute_work_hours(punch_in_dt, punched_at)
    overtime_minutes = compute_overtime_minutes(punched_at.astimezone(IST), shift_end)
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        current = db.attendance_logs.find_one({"_id": record["_id"]})
        if current is not None and current.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="that record is already punched out")
        raise HTTPException(status_code=409, detail="punch-out conflict")

    return serialize_attendance_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    existing_status = record.get("status")
    existing_punch_in = record.get("punch_in")
    existing_punch_out = record.get("punch_out")

    proposed_status = body.status if body.status is not None else existing_status
    proposed_punch_in = existing_punch_in if body.punch_in is None else dt_from_epoch_ms(body.punch_in)
    proposed_punch_out = existing_punch_out if body.punch_out is None else dt_from_epoch_ms(body.punch_out)

    if proposed_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot include punch times")
        proposed_punch_in = None
        proposed_punch_out = None
    else:
        if proposed_punch_in is None:
            if existing_punch_in is None:
                raise HTTPException(status_code=422, detail="presence status requires a punch_in")
            proposed_punch_in = existing_punch_in
        try:
            effective_date, _, _, _ = get_shift_context(employee, proposed_punch_in)
        except Exception as exc:  # pragma: no cover
            raise HTTPException(status_code=422, detail="invalid punch_in for attendance date") from exc
        if effective_date.isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
        if proposed_punch_out is not None:
            if proposed_punch_out <= proposed_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (proposed_punch_out - proposed_punch_in).total_seconds() > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        elif existing_punch_out is not None:
            proposed_punch_out = existing_punch_out

    if proposed_status in {"ABSENT", "LEAVE"}:
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if proposed_punch_in is None:
            final_work_hours = None
            final_late_minutes = 0
            final_overtime_minutes = 0
            final_half_day = False
        else:
            _, punch_ist, shift_start, shift_end = get_shift_context(employee, proposed_punch_in)
            final_late_minutes = compute_late_minutes(punch_ist, shift_start)
            if proposed_punch_out is None:
                final_work_hours = None
                final_overtime_minutes = 0
                final_half_day = False
            else:
                final_work_hours = compute_work_hours(proposed_punch_in, proposed_punch_out)
                final_overtime_minutes = compute_overtime_minutes(proposed_punch_out.astimezone(IST), shift_end)
                final_half_day = final_work_hours < 4.50

    new_values = {
        "status": proposed_status,
        "punch_in": proposed_punch_in,
        "punch_out": proposed_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    changes: dict[str, dict[str, Any]] = {}
    for field_name in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(field_name)
        new_value = new_values.get(field_name)
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": v["from"], "to": v["to"]} for k, v in changes.items()},
    }

    update_filter = {"_id": record["_id"], "history": record.get("history", [])}
    update_doc = {"$set": new_values, "$push": {"history": history_entry}}
    result = db.attendance_logs.update_one(update_filter, update_doc)
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="concurrent edit detected; history was updated by another request")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(status_code=404, detail="attendance record not found after update")
    return serialize_attendance_record(updated)
